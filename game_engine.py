import random
import json
import logging
import os
import re
import difflib
import yaml
from typing import TypedDict, List, Dict, Any
from langgraph.graph import StateGraph, END
from llm_factory import LLMFactory
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# [최적화] 프롬프트 캐시 (YAML 파일에서 한 번만 로드)
_prompt_cache: Dict[str, Any] = {}


def load_player_prompts() -> Dict[str, Any]:
    """플레이어 프롬프트 YAML 파일 로드 (캐싱)"""
    if 'player' not in _prompt_cache:
        prompt_path = os.path.join(os.path.dirname(__file__), 'config', 'prompt_player.yaml')
        try:
            with open(prompt_path, 'r', encoding='utf-8') as f:
                _prompt_cache['player'] = yaml.safe_load(f)
            logger.info(f"📄 [PROMPT] Loaded player prompts from {prompt_path}")
        except Exception as e:
            logger.error(f"Failed to load player prompts: {e}")
            _prompt_cache['player'] = {}
    return _prompt_cache['player']


# [최적화] LLM 인스턴스 캐시 (모델별로 재사용)
_llm_cache: Dict[str, Any] = {}
_llm_streaming_cache: Dict[str, Any] = {}


def get_cached_llm(api_key: str, model_name: str, streaming: bool = False):
    """LLM 인스턴스 캐싱으로 재생성 비용 절감"""
    cache = _llm_streaming_cache if streaming else _llm_cache
    cache_key = f"{model_name}_{streaming}"

    if cache_key not in cache:
        cache[cache_key] = LLMFactory.get_llm(
            api_key=api_key,
            model_name=model_name,
            streaming=streaming
        )
        logger.info(f"🔧 [LLM CACHE] Created new instance: {model_name} (streaming={streaming})")

    return cache[cache_key]


class PlayerState(TypedDict):
    scenario: Dict[str, Any]
    current_scene_id: str
    previous_scene_id: str
    player_vars: Dict[str, Any]
    history: List[str]
    last_user_choice_idx: int
    last_user_input: str
    parsed_intent: str
    system_message: str
    npc_output: str
    narrator_output: str
    critic_feedback: str
    retry_count: int
    chat_log_html: str
    near_miss_trigger: str  # [필수] Near Miss 저장용
    model: str  # [추가] 사용 중인 LLM 모델
    _internal_flags: Dict[str, Any]  # [추가] 내부 플래그 (UI에 노출 안 됨)


def normalize_text(text: str) -> str:
    """텍스트 정규화 (공백 제거, 소문자)"""
    return text.lower().replace(" ", "")


# --- Nodes ---

# 부정적 결말로 가는 transition 필터링 함수
def filter_negative_transitions(transitions: list, scenario: dict) -> list:
    """
    힌트 생성 시 부정적인 결말(ending, 패배, 죽음 등)로 가는 경로를 제외
    """
    negative_keywords = ['패배', '죽음', 'death', 'defeat', 'game_over', 'bad_end', '실패', '사망', '처치', '엔딩', 'ending', '종료', '끝', 'die', 'kill', 'dead', 'lose', 'lost']
    endings = {e['ending_id'].lower(): e for e in scenario.get('endings', [])}

    filtered = []
    for trans in transitions:
        target = trans.get('target_scene_id', '').lower()
        trigger = trans.get('trigger', '').lower()

        # 엔딩으로 가는 transition은 모두 제외 (긍/부정 무관)
        if target.startswith('ending') or target in endings:
            continue

        # trigger 자체에 부정적 키워드가 있으면 제외
        if any(kw in trigger for kw in negative_keywords):
            continue

        filtered.append(trans)

    return filtered if filtered else []  # 적합한 게 없으면 빈 리스트 반환


# 서사적 내레이션 힌트 (관찰자 시점) - YAML에서 로드
def get_narrative_hint_messages() -> List[str]:
    prompts = load_player_prompts()
    return prompts.get('narrative_hint_messages', [
        "주변의 공기가 긴장감으로 가득 차 있습니다. 무언가 눈에 띄는 것이 있을지도 모릅니다."
    ])


# 전투 씬 방어 행동 관련 내레이션 - YAML에서 로드
def get_battle_defensive_messages() -> List[str]:
    prompts = load_player_prompts()
    return prompts.get('battle_defensive_messages', [
        "당신은 몸을 낮추고 방어 자세를 취했습니다."
    ])


# Near Miss 상황용 서사적 힌트 - YAML에서 로드
def get_near_miss_narrative_hints() -> List[str]:
    prompts = load_player_prompts()
    return prompts.get('near_miss_narrative_hints', [
        "거의 통할 뻔했습니다. 무언가 반응이 있었습니다."
    ])


# 전투 씬 공격 행동 관련 내레이션 - YAML에서 로드
def get_battle_attack_messages() -> List[str]:
    prompts = load_player_prompts()
    return prompts.get('battle_attack_messages', [
        "당신의 공격이 적에게 닿았지만, 치명상을 입히지는 못했습니다."
    ])


# 전투 씬 교착 상태 내레이션 - YAML에서 로드
def get_battle_stalemate_messages() -> List[str]:
    prompts = load_player_prompts()
    return prompts.get('battle_stalemate_messages', [
        "치열한 공방이 이어집니다. 적도 당신도 결정타를 내지 못하고 있습니다."
    ])


def get_npc_weakness_hint(scenario: Dict[str, Any], enemy_names: List[str]) -> str:
    """
    NPC 데이터에서 약점을 찾아 서사적 힌트로 변환
    절대 직접적으로 '약점을 써라'라고 하지 않고, 환경 묘사로 힌트 제공
    """
    prompts = load_player_prompts()
    weakness_hints = prompts.get('weakness_hints', {})
    npcs = scenario.get('npcs', [])

    for npc in npcs:
        npc_name = npc.get('name', '')
        if npc_name in enemy_names:
            weakness = npc.get('weakness', npc.get('약점', ''))
            if weakness:
                weakness_lower = weakness.lower()

                if '소금' in weakness_lower or 'salt' in weakness_lower or '염' in weakness_lower:
                    hints = weakness_hints.get('salt', ["바닥에 쏟아진 짠물이 발밑에서 번들거립니다."])
                    return random.choice(hints)
                elif '빛' in weakness_lower or 'light' in weakness_lower:
                    hints = weakness_hints.get('light', ["천장의 조명이 깜빡이며 강렬한 빛을 내뿜습니다."])
                    return random.choice(hints)
                elif '불' in weakness_lower or 'fire' in weakness_lower or '화염' in weakness_lower:
                    hints = weakness_hints.get('fire', ["근처에 라이터가 떨어져 있습니다."])
                    return random.choice(hints)
                elif '물' in weakness_lower or 'water' in weakness_lower:
                    hints = weakness_hints.get('water', ["파열된 수도관에서 물이 뿜어져 나오고 있습니다."])
                    return random.choice(hints)
                elif '전기' in weakness_lower or 'electric' in weakness_lower:
                    hints = weakness_hints.get('electric', ["노출된 전선이 스파크를 일으키고 있습니다."])
                    return random.choice(hints)
                else:
                    default_hint = weakness_hints.get('default', "주변을 둘러보니, {weakness}과(와) 관련된 무언가가 눈에 들어옵니다.")
                    return default_hint.format(weakness=weakness)

    return ""


def check_victory_condition(user_input: str, scenario: Dict[str, Any], curr_scene: Dict[str, Any]) -> bool:
    """
    확실한 승리 조건이 만족되었는지 검사
    단순 '공격'만으로는 승리하지 않음 - 약점 활용이나 특수 조건 필요
    """
    transitions = curr_scene.get('transitions', [])
    user_lower = user_input.lower()

    # 적 정보 가져오기
    enemy_names = curr_scene.get('enemies', [])
    npcs = scenario.get('npcs', [])

    for npc in npcs:
        if npc.get('name', '') in enemy_names:
            weakness = npc.get('weakness', npc.get('약점', '')).lower()
            if weakness:
                # 약점이 입력에 포함되어 있으면 승리 조건 충족
                weakness_keywords = weakness.replace(',', ' ').replace('/', ' ').split()
                for kw in weakness_keywords:
                    if kw and len(kw) >= 2 and kw in user_lower:
                        return True

    # transition에 명시된 승리 trigger와 정확히 일치하는지 확인
    for trans in transitions:
        trigger = trans.get('trigger', '').lower()
        target = trans.get('target_scene_id', '').lower()

        # 긍정적 엔딩(승리)으로 가는 경로인지 확인
        if 'victory' in target or 'win' in target or '승리' in trigger:
            # 유사도가 매우 높을 때만 승리 인정 (0.8 이상)
            norm_input = normalize_text(user_input)
            norm_trigger = normalize_text(trigger)
            similarity = difflib.SequenceMatcher(None, norm_input, norm_trigger).ratio()
            if similarity >= 0.8:
                return True

    return False


def intent_parser_node(state: PlayerState):
    """
    [최적화됨] 의도 파서
    - LLM 호출 제거: 오직 파이썬 내부 연산(Fast-Track)만 수행하여 속도 극대화
    - 매칭 실패 시 -> 지체 없이 Chat/Hint 모드로 전환
    - [수정] 전투 씬에서 단순 공격은 바로 승리로 연결하지 않음
    """

    # 0. 상태 초기화 (중요: 이전 턴의 찌꺼기 제거)
    state['near_miss_trigger'] = None

    # 턴 시작 시 위치 기록
    if 'current_scene_id' in state:
        state['previous_scene_id'] = state['current_scene_id']

    user_input = state.get('last_user_input', '').strip()
    norm_input = normalize_text(user_input)
    logger.info(f"🟢 [USER INPUT]: {user_input}")

    if not user_input:
        state['parsed_intent'] = 'chat'
        state['system_message'] = "행동을 입력해주세요."
        return state

    # 시스템적 선택 처리
    if state.get('last_user_choice_idx', -1) != -1:
        state['parsed_intent'] = 'transition'
        return state

    scenario = state['scenario']
    curr_scene_id = state['current_scene_id']
    scenes = {s['scene_id']: s for s in scenario.get('scenes', [])}

    curr_scene = scenes.get(curr_scene_id)
    if not curr_scene:
        state['parsed_intent'] = 'chat'
        return state

    # 엔딩 체크
    endings = {e['ending_id']: e for e in scenario.get('endings', [])}
    if curr_scene_id in endings:
        state['parsed_intent'] = 'ending'
        return state

    transitions = curr_scene.get('transitions', [])
    if not transitions:
        state['parsed_intent'] = 'chat'
        return state

    # [신규] 전투 씬 감지 및 공격 행동 처리
    scene_type = curr_scene.get('type', 'normal')
    attack_keywords = ['공격', '때리', '치', '베', '찌르', '쏘', '던지', '싸우', 'attack', 'hit', 'strike', 'fight', 'kill', '처치', '죽이', '무찌']
    is_attack_action = any(kw in user_input.lower() for kw in attack_keywords)

    if scene_type == 'battle' and is_attack_action:
        # 승리 조건 확인
        if not check_victory_condition(user_input, scenario, curr_scene):
            # 승리 조건 미충족 -> 전투 지속 (chat 모드로 유지하되 전투 묘사)
            logger.info(f"⚔️ [BATTLE] Attack detected but victory condition not met. Continuing battle.")
            state['parsed_intent'] = 'chat'
            state['_internal_flags'] = state.get('_internal_flags', {})
            state['_internal_flags']['battle_attack'] = True
            return state

    # 🚀 [SPEED-UP] Fast-Track 매칭
    best_idx = -1
    highest_ratio = 0.0
    best_trigger_text = ""

    for idx, trans in enumerate(transitions):
        trigger = trans.get('trigger', '').strip()
        if not trigger: continue
        norm_trigger = normalize_text(trigger)
        target = trans.get('target_scene_id', '').lower()

        # [수정] 전투 씬에서 엔딩으로 가는 transition은 높은 유사도 요구
        is_ending_transition = target.startswith('ending') or target in endings

        # 1. 완전 포함 관계 확인 (가장 확실함 -> 즉시 리턴 가능)
        if norm_input in norm_trigger or norm_trigger in norm_input:
            if len(norm_input) >= 2:
                # [수정] 전투 씬에서 엔딩 transition은 승리 조건 체크
                if scene_type == 'battle' and is_ending_transition:
                    if not check_victory_condition(user_input, scenario, curr_scene):
                        continue  # 승리 조건 미충족 시 이 transition 건너뜀

                logger.info(f"⚡ [FAST-TRACK] Direct Match: '{user_input}' matched '{trigger}'")
                state['last_user_choice_idx'] = idx
                state['parsed_intent'] = 'transition'
                return state

        # 2. 유사도 계산 (Best Match 찾기 위해 루프 돎)
        similarity = difflib.SequenceMatcher(None, norm_input, norm_trigger).ratio()

        # [수정] 전투 씬에서 엔딩 transition은 더 높은 threshold 요구
        if scene_type == 'battle' and is_ending_transition:
            if similarity < 0.8:  # 엔딩은 0.8 이상 필요
                continue

        if similarity > highest_ratio:
            highest_ratio = similarity
            best_idx = idx
            best_trigger_text = trigger

    # [수정] 루프 종료 후 '가장 높은 점수'로 최종 판단
    # 0.6 이상: 성공
    if highest_ratio >= 0.6:
        target_trans = transitions[best_idx]
        target = target_trans.get('target_scene_id', '').lower()
        is_ending_transition = target.startswith('ending') or target in endings

        # [수정] 전투 씬에서 엔딩으로 가려면 승리 조건 충족 필요
        if scene_type == 'battle' and is_ending_transition:
            if not check_victory_condition(user_input, scenario, curr_scene):
                logger.info(f"⚔️ [BATTLE] Fuzzy match to ending blocked - victory condition not met")
                state['parsed_intent'] = 'chat'
                state['_internal_flags'] = state.get('_internal_flags', {})
                state['_internal_flags']['battle_attack'] = True
                return state

        logger.info(f"⚡ [FAST-TRACK] Fuzzy Match ({highest_ratio:.2f}): '{user_input}' -> '{best_trigger_text}'")
        state['last_user_choice_idx'] = best_idx
        state['parsed_intent'] = 'transition'
        return state

    # 0.4 ~ 0.59: 아까운 실패 (Near Miss)
    elif highest_ratio >= 0.4:
        logger.info(f"⚡ [FAST-TRACK] Near Miss ({highest_ratio:.2f}): '{user_input}' vs '{best_trigger_text}'")
        state['near_miss_trigger'] = best_trigger_text
        state['parsed_intent'] = 'chat'
        return state

    # 매칭 실패 -> 일반 채팅/힌트
    state['parsed_intent'] = 'chat'
    return state


def rule_node(state: PlayerState):
    """규칙 엔진 (이동 및 상태 변경)"""
    # ... (기존 코드 동일) ...
    idx = state['last_user_choice_idx']
    scenario = state['scenario']
    curr_scene_id = state['current_scene_id']

    all_scenes = {s['scene_id']: s for s in scenario['scenes']}
    all_endings = {e['ending_id']: e for e in scenario.get('endings', [])}

    sys_msg = []
    curr_scene = all_scenes.get(curr_scene_id)
    transitions = curr_scene.get('transitions', []) if curr_scene else []

    if state['parsed_intent'] == 'transition' and 0 <= idx < len(transitions):
        trans = transitions[idx]
        effects = trans.get('effects', [])
        next_id = trans.get('target_scene_id')

        # 이펙트 적용
        for eff in effects:
            try:
                if isinstance(eff, dict):
                    key = eff.get("target", "").lower()
                    operation = eff.get("operation", "add")
                    raw_val = eff.get("value", 0)

                    val = 0
                    if isinstance(raw_val, (int, float)):
                        val = int(raw_val)
                    elif isinstance(raw_val, str) and raw_val.isdigit():
                        val = int(raw_val)

                    if operation in ["gain_item", "lose_item"]:
                        item_name = str(eff.get("value", ""))
                        inventory = state['player_vars'].get('inventory', [])
                        if operation == "gain_item" and item_name not in inventory:
                            inventory.append(item_name)
                            sys_msg.append(f"📦 획득: {item_name}")
                        elif operation == "lose_item" and item_name in inventory:
                            inventory.remove(item_name)
                            sys_msg.append(f"🗑️ 사용: {item_name}")
                        state['player_vars']['inventory'] = inventory
                        continue

                    if key:
                        current_val = state['player_vars'].get(key, 0)
                        if not isinstance(current_val, (int, float)): current_val = 0
                        if operation == "add":
                            state['player_vars'][key] = current_val + val
                            sys_msg.append(f"{key.upper()} +{val}")
                        elif operation == "subtract":
                            state['player_vars'][key] = max(0, current_val - val)
                            sys_msg.append(f"{key.upper()} -{val}")
                        elif operation == "set":
                            state['player_vars'][key] = val
                            sys_msg.append(f"{key.upper()} = {val}")
            except Exception:
                pass

        # 씬 이동
        if next_id:
            state['current_scene_id'] = next_id
            logger.info(f"👣 [MOVE] {curr_scene_id} -> {next_id}")

    # 엔딩 체크
    if state['current_scene_id'] in all_endings:
        ending = all_endings[state['current_scene_id']]
        state['parsed_intent'] = 'ending'
        state['narrator_output'] = f"""
        <div class="my-8 p-8 border-2 border-yellow-500/50 bg-gradient-to-b from-yellow-900/40 to-black rounded-xl text-center fade-in shadow-2xl relative overflow-hidden">
            <h3 class="text-3xl font-black text-yellow-400 mb-4 tracking-[0.2em] uppercase drop-shadow-md">🎉 ENDING 🎉</h3>
            <div class="w-16 h-1 bg-yellow-500 mx-auto mb-6 rounded-full"></div>
            <div class="text-2xl font-bold text-white mb-4 drop-shadow-sm">"{ending.get('title')}"</div>
            <p class="text-gray-200 leading-relaxed text-lg serif-font">
                {ending.get('description')}
            </p>
        </div>
        """

    state['system_message'] = " | ".join(sys_msg)
    return state


def npc_node(state: PlayerState):
    """NPC 대화 (이동 아닐 때만 발동)"""
    if state.get('parsed_intent') != 'chat':
        state['npc_output'] = ""
        return state

    scenario = state['scenario']
    curr_id = state['current_scene_id']
    all_scenes = {s['scene_id']: s for s in scenario['scenes']}
    curr_scene = all_scenes.get(curr_id)
    npc_names = curr_scene.get('npcs', []) if curr_scene else []

    if not npc_names:
        state['npc_output'] = ""
        return state

    target_npc_name = npc_names[0]
    npc_info = {"name": target_npc_name, "role": "Unknown", "personality": "보통"}

    for npc in scenario.get('npcs', []):
        if npc.get('name') == target_npc_name:
            npc_info['role'] = npc.get('role', 'Unknown')
            npc_info['personality'] = npc.get('personality', '보통')
            npc_info['dialogue_style'] = npc.get('dialogue_style', '')
            break

    history = state.get('history', [])
    history_context = "\n".join(history[-3:]) if history else "대화 시작"
    user_input = state['last_user_input']

    # YAML에서 프롬프트 로드
    prompts = load_player_prompts()
    prompt_template = prompts.get('npc_dialogue', '')

    if prompt_template:
        prompt = prompt_template.format(
            npc_name=npc_info['name'],
            npc_role=npc_info['role'],
            npc_personality=npc_info['personality'],
            history_context=history_context,
            user_input=user_input
        )
    else:
        # 폴백 프롬프트
        prompt = f"""당신은 텍스트 RPG의 NPC입니다.

**NPC 정보:**
- 이름: {npc_info['name']}
- 역할: {npc_info['role']}
- 성격: {npc_info['personality']}

**대화 맥락:**
{history_context}

**플레이어의 말/행동:**
"{user_input}"

**이제 NPC {npc_info['name']}로서 응답하세요:**"""

    try:
        api_key = os.getenv("OPENROUTER_API_KEY")
        model_name = state.get('model', 'openai/tngtech/deepseek-r1t2-chimera:free')
        llm = get_cached_llm(api_key=api_key, model_name=model_name, streaming=False)
        response = llm.invoke(prompt).content.strip()

        # [추가] 응답 검증 - 사용자 입력을 그대로 반복하는 경우 필터링
        normalized_input = user_input.lower().replace(" ", "")
        normalized_response = response.lower().replace(" ", "")

        if normalized_input in normalized_response and len(normalized_response) < len(normalized_input) + 10:
            # 사용자 입력을 단순 반복한 경우 기본 응답 생성
            logger.warning(f"⚠️ NPC response too similar to user input, using fallback")
            response = f"(잠시 생각하더니) 알겠습니다."

        state['npc_output'] = response

        if 'history' not in state: state['history'] = []
        state['history'].append(f"User: {user_input}")
        state['history'].append(f"NPC({target_npc_name}): {response}")

        logger.info(f"💬 [NPC] {target_npc_name}: {response}")
    except Exception as e:
        logger.error(f"NPC generation error: {e}")
        state['npc_output'] = f"(말없이 고개를 끄덕입니다)"

    return state


def check_npc_appearance(state: PlayerState) -> str:
    """NPC 및 적 등장 (템플릿 기반)"""
    scenario = state['scenario']
    curr_id = state['current_scene_id']

    # 씬 변경 없으면 등장 메시지 생략
    if state.get('previous_scene_id') == curr_id:
        return ""

    all_scenes = {s['scene_id']: s for s in scenario['scenes']}
    curr_scene = all_scenes.get(curr_id)
    if not curr_scene: return ""

    # [FIX] NPC와 적을 모두 처리
    npc_names = curr_scene.get('npcs', [])
    enemy_names = curr_scene.get('enemies', [])
    scene_type = curr_scene.get('type', 'normal')  # [FIX] 장면 유형 확인

    if not npc_names and not enemy_names: return ""

    scene_history_key = f"npc_appeared_{curr_id}"
    player_vars = state.get('player_vars', {})
    if player_vars.get(scene_history_key): return ""

    state['player_vars'][scene_history_key] = True
    introductions = []

    # [FIX] 장면 유형에 따른 메시지 차별화
    if scene_type == 'battle':
        introductions.append("""
        <div class='battle-alert text-red-400 font-bold my-3 p-3 bg-red-900/30 rounded border-2 border-red-500 animate-pulse'>
            ⚔️ 전투 시작! 적과의 전투가 시작됩니다!
        </div>
        """)

    # NPC 등장
    if npc_names:
        npc_action_templates = [
            "당신을 바라봅니다.", "무언가를 하고 있습니다.", "조용히 서 있습니다.",
            "경계하는 눈빛입니다.", "당신을 흥미롭게 쳐다봅니다."
        ]
        for npc_name in npc_names:
            action = random.choice(npc_action_templates)
            intro_html = f"""
            <div class='npc-intro text-green-300 italic my-2 p-2 bg-green-900/20 rounded border-l-2 border-green-500'>
                👀 <span class='font-bold'>{npc_name}</span>이(가) {action}
            </div>
            """
            introductions.append(intro_html)

    # [FIX] 적 등장 처리
    if enemy_names:
        enemy_action_templates = [
            "적대적인 기색을 보입니다!", "공격 태세를 갖춥니다!", "위협적으로 다가옵니다!",
            "살기를 내뿜습니다!", "전투를 준비합니다!"
        ]
        for enemy_name in enemy_names:
            action = random.choice(enemy_action_templates)
            intro_html = f"""
            <div class='enemy-intro text-red-400 font-bold my-2 p-2 bg-red-900/30 rounded border-l-2 border-red-500'>
                ⚔️ <span class='font-bold'>{enemy_name}</span>이(가) 나타났습니다! {action}
            </div>
            """
            introductions.append(intro_html)

    return "\n".join(introductions)


def narrator_node(state: PlayerState):
    return state


# --- Streaming Generators (SSE) ---

def prologue_stream_generator(state: PlayerState):
    scenario = state['scenario']
    prologue_text = scenario.get('prologue', scenario.get('prologue_text', ''))
    if not prologue_text:
        yield "이야기가 시작됩니다..."
        return
    yield prologue_text


def get_narrative_fallback_message(scenario: Dict[str, Any]) -> str:
    """세계관별 폴백 메시지 - YAML에서 로드"""
    genre = scenario.get('genre', '').lower()
    world_setting = scenario.get('world_setting', '').lower()

    # YAML에서 폴백 메시지 로드
    prompts = load_player_prompts()
    fallback_messages = prompts.get('fallback_messages', {})

    if not fallback_messages:
        # 기본 폴백 메시지
        fallback_messages = {
            'cyberpunk': "⚠️ 신경 신호가 불안정하여 시야가 일시적으로 차단되었습니다. 잠시 후 다시 시도하십시오.",
            'sf': "⚠️ 통신 간섭이 감지되었습니다. 신호가 안정화될 때까지 대기해 주세요.",
            'fantasy': "⚠️ 마력의 흐름이 일시적으로 혼란스럽습니다. 잠시 정신을 가다듬어 주세요.",
            'horror': "⚠️ 알 수 없는 힘이 시야를 가립니다... 잠시 후 다시 시도해 주세요.",
            'modern': "⚠️ 잠시 정신이 혼미해졌습니다. 심호흡을 하고 다시 시도해 주세요.",
            'medieval': "⚠️ 갑작스러운 현기증이 엄습합니다. 잠시 쉬었다가 다시 시도해 주세요.",
            'apocalypse': "⚠️ 방사능 간섭으로 인해 감각이 일시적으로 마비되었습니다. 잠시 후 다시 시도하십시오.",
            'workplace': "⚠️ 과로로 인해 잠시 멍해졌습니다. 커피를 마시고 다시 시도해 주세요.",
            'martial': "⚠️ 내공의 흐름이 일시적으로 막혔습니다. 기를 가다듬고 다시 시도하십시오.",
            'default': "⚠️ 잠시 상황 파악이 어렵습니다. 심호흡을 하고 다시 시도해 주세요."
        }

    for key, message in fallback_messages.items():
        if key != 'default' and (key in genre or key in world_setting):
            return message

    return fallback_messages.get('default', "⚠️ 잠시 상황 파악이 어렵습니다. 심호흡을 하고 다시 시도해 주세요.")


def scene_stream_generator(state: PlayerState, retry_count: int = 0, max_retries: int = 2):
    """
    나레이션 스트리밍
    [MODE 1] 힌트 모드 (이동 X)
    [MODE 2] 묘사 모드 (이동 O)
    [MODE 3] 전투 지속 모드 (battle 씬에서 chat일 때)
    """
    scenario = state['scenario']
    curr_id = state['current_scene_id']
    prev_id = state.get('previous_scene_id')
    user_input = state.get('last_user_input', '')

    all_scenes = {s['scene_id']: s for s in scenario['scenes']}
    all_endings = {e['ending_id']: e for e in scenario.get('endings', [])}

    if curr_id in all_endings:
        ending = all_endings[curr_id]
        yield f"""
        <div class="ending-scene">
            <h3>🎉 {ending.get('title', 'ENDING')} 🎉</h3>
            <p>{ending.get('description', '')}</p>
        </div>
        """
        return

    curr_scene = all_scenes.get(curr_id)

    if not curr_scene:
        logger.warning(f"Scene not found: {curr_id}")
        if retry_count < max_retries:
            yield f"__RETRY_SIGNAL__"
            return
        fallback_msg = get_narrative_fallback_message(scenario)
        yield f"""
        <div class="bg-yellow-900/30 border border-yellow-700/50 rounded-lg p-4 my-2">
            <div class="text-yellow-400 serif-font">{fallback_msg}</div>
        </div>
        """
        start_scene_id = scenario.get('start_scene_id')
        if start_scene_id and start_scene_id in all_scenes:
            state['current_scene_id'] = start_scene_id
        return

    scene_title = curr_scene.get('title', 'Untitled')
    scene_type = curr_scene.get('type', 'normal')
    transitions = curr_scene.get('transitions', [])
    enemy_names = curr_scene.get('enemies', [])

    # [MODE 1] 씬 유지됨 (탐색/대화) -> 힌트 모드
    if prev_id == curr_id and user_input:
        internal_flags = state.get('_internal_flags', {})
        is_battle_attack = internal_flags.get('battle_attack', False)

        # [신규 MODE 3] 전투 씬에서 공격 행동 - 전투 지속 묘사
        if scene_type == 'battle' and is_battle_attack:
            # 플래그 초기화
            state['_internal_flags']['battle_attack'] = False

            # [개선] LLM으로 즉각적인 물리적 결과 생성
            attack_result_prompt = f"""당신은 텍스트 RPG의 게임 마스터입니다.

**최우선 지침: 유저의 마지막 입력("{user_input}")에 대한 즉각적이고 구체적인 물리적 결과를 먼저 서술하세요.**

**현재 상황:**
- 장면: "{scene_title}" (전투 중)
- 유저의 행동: "{user_input}"
- 결과: 공격이 적에게 닿았으나 치명타는 아님

**약점 정보 (환경 묘사에 자연스럽게 포함할 것):**
{get_npc_weakness_hint(scenario, enemy_names) or "주변을 살펴보니 활용할 수 있는 것이 보입니다."}

**규칙:**
1. 먼저 유저 행동의 물리적 결과를 2인칭으로 서술 ("당신의 공격이...")
2. 그 다음 환경 묘사를 통해 약점을 암시
3. 2-3문장, 한국어로 작성
4. "~해보세요", "전략이 필요합니다" 등 금지

**응답:**"""

            try:
                api_key = os.getenv("OPENROUTER_API_KEY")
                model_name = state.get('model', 'openai/tngtech/deepseek-r1t2-chimera:free')
                llm = get_cached_llm(api_key=api_key, model_name=model_name, streaming=True)
                for chunk in llm.stream(attack_result_prompt):
                    if chunk.content: yield chunk.content
            except Exception:
                yield random.choice(get_battle_attack_messages())
            return

        # [개선] 전투 씬에서 조사/탐색 행동 감지 - 약점 노출 강화
        investigation_keywords = ['조사', '살펴', '찾', '둘러', '관찰', '확인', '생각', '방법', '전략', '약점', '탐색', 'look', 'search', 'examine', 'think', 'find']
        is_investigation = any(kw in user_input.lower() for kw in investigation_keywords)

        if scene_type == 'battle' and is_investigation:
            # [필수] 약점을 명확히 보여주는 환경 묘사 생성
            weakness_hint = get_npc_weakness_hint(scenario, enemy_names)

            investigation_prompt = f"""당신은 텍스트 RPG의 게임 마스터입니다.

**최우선 지침: 유저의 마지막 입력("{user_input}")에 대한 즉각적이고 구체적인 물리적 결과를 먼저 서술하세요.**

**현재 상황:**
- 장면: "{scene_title}" (전투 중)
- 유저의 행동: "{user_input}" (주변 조사/탐색)

**필수 약점 힌트 (반드시 포함):**
{weakness_hint if weakness_hint else "주변을 살펴보니 특이한 물건이 눈에 띕니다."}

**규칙:**
1. 먼저 유저의 조사 행동 결과를 2인칭으로 서술
2. 위 약점 힌트를 구체적인 오브젝트로 묘사 (예: "바닥에 쏟아진 소금물이 보입니다")
3. 2-3문장, 한국어
4. 절대 금지: "전략이 필요합니다", "방법을 찾아보세요" 등 메타적 제안

**응답:**"""

            try:
                api_key = os.getenv("OPENROUTER_API_KEY")
                model_name = state.get('model', 'openai/tngtech/deepseek-r1t2-chimera:free')
                llm = get_cached_llm(api_key=api_key, model_name=model_name, streaming=True)
                for chunk in llm.stream(investigation_prompt):
                    if chunk.content: yield chunk.content
            except Exception:
                # 폴백: 약점 힌트 직접 출력
                if weakness_hint:
                    yield f"주변을 살핍니다. {weakness_hint}"
                else:
                    yield "주변을 둘러보니 활용할 수 있는 것들이 보입니다."
            return

        # [개선] 방어 행동 감지 (전투 씬에서)
        defensive_keywords = ['방어', '회피', '막', '피하', '버티', '숨', '엄폐', '도망', '후퇴', '수비', 'block', 'defend', 'dodge', 'hide', 'retreat']
        is_defensive_action = any(kw in user_input.lower() for kw in defensive_keywords)

        if scene_type == 'battle' and is_defensive_action:
            # [개선] LLM으로 방어 결과 생성
            defense_prompt = f"""당신은 텍스트 RPG의 게임 마스터입니다.

**최우선 지침: 유저의 마지막 입력("{user_input}")에 대한 즉각적이고 구체적인 물리적 결과를 먼저 서술하세요.**

**현재 상황:**
- 장면: "{scene_title}" (전투 중)
- 유저의 행동: "{user_input}" (방어적 행동)

**약점 정보 (환경 묘사에 자연스럽게 포함):**
{get_npc_weakness_hint(scenario, enemy_names) or "주변에 활용할 수 있는 것들이 있습니다."}

**규칙:**
1. 먼저 방어 행동의 즉각적 결과를 서술 ("당신은 몸을 낮췄습니다...")
2. 숨 돌리는 동안 주변 환경(약점)이 눈에 들어오는 식으로 묘사
3. 2-3문장, 한국어

**응답:**"""

            try:
                api_key = os.getenv("OPENROUTER_API_KEY")
                model_name = state.get('model', 'openai/tngtech/deepseek-r1t2-chimera:free')
                llm = get_cached_llm(api_key=api_key, model_name=model_name, streaming=True)
                for chunk in llm.stream(defense_prompt):
                    if chunk.content: yield chunk.content
            except Exception:
                yield random.choice(get_battle_defensive_messages())
            return

        # [개선] Near Miss 감지 시 서사적 힌트 반환 (LLM 사용)
        near_miss = state.get('near_miss_trigger')
        if near_miss:
            near_miss_prompt = f"""당신은 텍스트 RPG의 게임 마스터입니다.

**최우선 지침: 유저의 마지막 입력("{user_input}")에 대한 즉각적이고 구체적인 물리적 결과를 먼저 서술하세요.**

**상황:**
- 유저 시도: "{user_input}"
- 정답에 가까움: "{near_miss}"
- 결과: 아슬아슬하게 실패

**규칙:**
1. 유저 행동의 물리적 결과를 먼저 서술
2. "거의 통할 뻔했다", "방향은 맞다" 등의 긍정적 피드백
3. 1-2문장, 한국어

**응답:**"""

            try:
                api_key = os.getenv("OPENROUTER_API_KEY")
                model_name = state.get('model', 'openai/tngtech/deepseek-r1t2-chimera:free')
                llm = get_cached_llm(api_key=api_key, model_name=model_name, streaming=True)
                for chunk in llm.stream(near_miss_prompt):
                    if chunk.content: yield chunk.content
            except Exception:
                yield random.choice(get_near_miss_narrative_hints())
            return

        # [최적화] NPC 대화 있으면 스킵
        npc_output = state.get('npc_output', '')
        if npc_output:
            yield ""
            return

        # [신규] 전투 씬에서 일반 행동 시에도 전투 상황 유지 (LLM 사용)
        if scene_type == 'battle':
            battle_continue_prompt = f"""당신은 텍스트 RPG의 게임 마스터입니다.

**최우선 지침: 유저의 마지막 입력("{user_input}")에 대한 즉각적이고 구체적인 물리적 결과를 먼저 서술하세요.**

**현재 상황:**
- 장면: "{scene_title}" (전투 교착 상태)
- 유저의 행동: "{user_input}"

**약점 정보:**
{get_npc_weakness_hint(scenario, enemy_names) or "주변에 활용할 수 있는 것이 있습니다."}

**규칙:**
1. 유저 행동의 즉각적 결과 서술
2. 전투 긴장감 유지하며 환경에 약점 암시
3. 2-3문장, 한국어

**응답:**"""

            try:
                api_key = os.getenv("OPENROUTER_API_KEY")
                model_name = state.get('model', 'openai/tngtech/deepseek-r1t2-chimera:free')
                llm = get_cached_llm(api_key=api_key, model_name=model_name, streaming=True)
                for chunk in llm.stream(battle_continue_prompt):
                    if chunk.content: yield chunk.content
            except Exception:
                yield random.choice(get_battle_stalemate_messages())
            return

        # [삭제] 정적 힌트 로직 완전 제거 - 모든 응답은 LLM으로 일원화

        # [개선] 부정적 결말로 가는 transition 완전 필터링
        filtered_transitions = filter_negative_transitions(transitions, scenario)
        filtered_hints = [t.get('trigger', '') for t in filtered_transitions if t.get('trigger')]
        hint_list = ', '.join([f'"{h}"' for h in filtered_hints[:3]]) if filtered_hints else '없음'

        # YAML에서 힌트 모드 프롬프트 로드
        prompts = load_player_prompts()
        hint_prompt_template = prompts.get('hint_mode', '')

        if hint_prompt_template:
            # [개선] 프롬프트 최상단에 유저 입력에 대한 즉각 응답 지침 추가
            prompt = f"""**최우선 지침: 유저의 마지막 입력("{user_input}")에 대한 즉각적이고 구체적인 물리적 결과를 먼저 서술하세요.**

""" + hint_prompt_template.format(
                scene_title=scene_title,
                user_input=user_input,
                hint_list=hint_list
            )
        else:
            # 폴백 프롬프트
            prompt = f"""**최우선 지침: 유저의 마지막 입력("{user_input}")에 대한 즉각적이고 구체적인 물리적 결과를 먼저 서술하세요.**

당신은 텍스트 기반 RPG의 게임 마스터입니다. 철저히 세계관 안에서 상황을 묘사하는 역할입니다.

**현재 상황:**
- 장면: "{scene_title}"
- 플레이어의 행동: "{user_input}"
- 결과: 행동이 장면 전환을 유발하지 않음

**가능한 행동 방향 (참고용, 절대 직접 언급 금지):**
{hint_list}

**이제 게임 마스터로서 상황을 묘사하세요:**"""

        try:
            api_key = os.getenv("OPENROUTER_API_KEY")
            model_name = state.get('model', 'openai/tngtech/deepseek-r1t2-chimera:free')
            llm = get_cached_llm(api_key=api_key, model_name=model_name, streaming=True)
            for chunk in llm.stream(prompt):
                if chunk.content: yield chunk.content
        except Exception:
            # 폴백: 기본 서사적 메시지
            yield "당신의 행동에 주변이 미세하게 반응했습니다. 더 주의 깊게 상황을 살펴봅니다."
        return

    # [MODE 2] 씬 변경됨 -> 전체 묘사
    scene_desc = curr_scene.get('description', '')
    npc_names = curr_scene.get('npcs', [])

    npc_intro = check_npc_appearance(state)
    if npc_intro: yield npc_intro + "<br><br>"

    # YAML에서 씬 묘사 프롬프트 로드
    npc_list = ', '.join(npc_names) if npc_names else '없음'
    prompts = load_player_prompts()
    scene_prompt_template = prompts.get('scene_description', '')

    if scene_prompt_template:
        # [개선] 씬 변경 시에도 유저 입력 컨텍스트 포함
        if user_input:
            context_prefix = f"""**최우선 지침: 유저의 마지막 입력("{user_input}")이 이 장면으로의 전환을 일으켰습니다. 그 결과를 먼저 서술하세요.**

"""
            prompt = context_prefix + scene_prompt_template.format(
                scene_title=scene_title,
                scene_desc=scene_desc,
                npc_list=npc_list
            )
        else:
            prompt = scene_prompt_template.format(
                scene_title=scene_title,
                scene_desc=scene_desc,
                npc_list=npc_list
            )
    else:
        # 폴백 프롬프트
        prompt = f"""당신은 텍스트 기반 RPG의 게임 마스터입니다.

**장면 정보:**
- 제목: "{scene_title}"
- 설명: "{scene_desc}"
- 등장 NPC: {npc_list}


**이제 장면을 묘사하세요:**"""

    try:
        api_key = os.getenv("OPENROUTER_API_KEY")
        model_name = state.get('model', 'openai/tngtech/deepseek-r1t2-chimera:free')
        # [최적화] 캐시된 LLM 사용
        llm = get_cached_llm(api_key=api_key, model_name=model_name, streaming=True)

        accumulated_text = ""
        has_content = False

        for chunk in llm.stream(prompt):
            if chunk.content:
                accumulated_text += chunk.content
                has_content = True
                yield chunk.content

        if not has_content or len(accumulated_text.strip()) < 10:
            raise Exception("Empty or insufficient response from LLM")

    except Exception as e:
        logger.error(f"Scene Streaming Error (attempt {retry_count + 1}): {e}")

        if retry_count < max_retries:
            yield f"__RETRY_SIGNAL__"
            return

        fallback_msg = get_narrative_fallback_message(scenario)

        if scene_desc:
            yield f"""
            <div class="bg-yellow-900/30 border border-yellow-700/50 rounded-lg p-4 my-2">
                <div class="text-yellow-400 serif-font mb-2">{fallback_msg}</div>
            </div>
            <div class="text-gray-300 serif-font">{scene_desc}</div>
            """
        else:
            yield f"""
            <div class="bg-yellow-900/30 border border-yellow-700/50 rounded-lg p-4 my-2">
                <div class="text-yellow-400 serif-font">{fallback_msg}</div>
            </div>
            """

def create_game_graph():
    # ... (기존 코드 동일) ...
    workflow = StateGraph(PlayerState)
    workflow.add_node("intent_parser", intent_parser_node)
    workflow.add_node("rule_engine", rule_node)
    workflow.add_node("npc_actor", npc_node)
    workflow.add_node("narrator", narrator_node)

    workflow.set_entry_point("intent_parser")

    def route_action(state):
        intent = state.get('parsed_intent')
        if intent == 'transition' or intent == 'ending':
            return "rule_engine"
        else:
            return "npc_actor"

    workflow.add_conditional_edges("intent_parser", route_action,
                                   {"rule_engine": "rule_engine", "npc_actor": "npc_actor"})
    workflow.add_edge("rule_engine", "narrator")
    workflow.add_edge("npc_actor", "narrator")
    workflow.add_edge("narrator", END)

    return workflow.compile()