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
from core.state import WorldState

load_dotenv()

logger = logging.getLogger(__name__)

# [최적화] 시나리오 데이터 캐시
_scenario_cache: Dict[int, Dict[str, Any]] = {}


def get_scenario_by_id(scenario_id: int) -> Dict[str, Any]:
    """
    시나리오 ID로 데이터 조회 (캐싱)
    PlayerState에서 시나리오 전체 데이터를 제거하고 필요 시 이 함수로 조회
    """
    if scenario_id in _scenario_cache:
        return _scenario_cache[scenario_id]

    # DB에서 조회
    from models import SessionLocal, Scenario

    db = SessionLocal()
    try:
        scenario = db.query(Scenario).filter(Scenario.id == scenario_id).first()
        if scenario:
            scenario_data = scenario.data

            # [Fix] 중첩된 scenario 구조 처리
            if 'scenario' in scenario_data and isinstance(scenario_data['scenario'], dict):
                scenario_data = scenario_data['scenario']

            # [Fix] 필수 키가 없으면 기본값 설정
            if 'scenes' not in scenario_data:
                scenario_data['scenes'] = []
            if 'endings' not in scenario_data:
                scenario_data['endings'] = []

            _scenario_cache[scenario_id] = scenario_data
            return scenario_data
        else:
            logger.error(f"❌ Scenario not found: {scenario_id}")
            return {'scenes': [], 'endings': []}
    except Exception as e:
        logger.error(f"❌ Failed to load scenario {scenario_id}: {e}")
        return {'scenes': [], 'endings': []}
    finally:
        db.close()


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
    scenario_id: int  # [경량화] 시나리오 전체 대신 ID만 저장
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
    stuck_count: int  # [추가] 정체 상태 카운터 (장면 전환 실패 횟수)
    _internal_flags: Dict[str, Any]  # [추가] 내부 플래그 (UI에 노출 안 됨)


def normalize_text(text: str) -> str:
    """텍스트 정규화 (공백 제거, 소문자)"""
    return text.lower().replace(" ", "")


def format_player_status(scenario: Dict[str, Any], player_vars: Dict[str, Any] = None) -> str:
    """
    플레이어 현재 상태를 포맷팅 (인벤토리 포함)
    player_vars가 제공되면 실제 플레이어 상태를 사용, 없으면 초기 상태 사용
    """
    if player_vars:
        # 실제 플레이어 상태 사용
        current_state = player_vars
    else:
        # 초기 상태 구성
        initial_state = {}

        # 1. variables 필드에서 초기 상태 구성
        if 'variables' in scenario and isinstance(scenario['variables'], list):
            for var in scenario['variables']:
                if isinstance(var, dict) and 'name' in var and 'initial_value' in var:
                    var_name = var['name'].lower()
                    initial_state[var_name] = var['initial_value']

        # 2. initial_state 필드도 확인 (하위 호환성)
        if 'initial_state' in scenario:
            initial_state.update(scenario['initial_state'])

        current_state = initial_state

    # 상태가 비어있으면 빈 문자열 반환
    if not current_state:
        return "초기 상태 없음"

    status_lines = []
    inventory = current_state.get('inventory', [])

    for key, value in current_state.items():
        if key == 'inventory':
            continue
        if isinstance(value, (int, float)):
            status_lines.append(f"- {key}: {value}")
        elif isinstance(value, str):
            status_lines.append(f"- {key}: {value}")

    # 인벤토리는 마지막에 추가 (강조)
    if inventory and isinstance(inventory, list):
        items_str = ', '.join([str(item) for item in inventory])
        status_lines.append(f"- 🎒 소지품 (인벤토리): [{items_str}]")
    else:
        status_lines.append(f"- 🎒 소지품 (인벤토리): [비어 있음]")

    return '\n  '.join(status_lines)


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
    [2단계 API 호출 구조로 변경됨]
    1단계: LLM을 통한 의도 분류 (intent_classifier)
    - transitions 목록을 참고하여 유저 입력의 의도를 파악
    - transition/chat/investigate/attack/defend 등으로 분류
    """

    # 0. 상태 초기화 (중요: 이전 턴의 찌꺼기 제거)
    state['near_miss_trigger'] = None

    # 턴 시작 시 위치 기록
    if 'current_scene_id' in state:
        state['previous_scene_id'] = state['current_scene_id']

    user_input = state.get('last_user_input', '').strip()
    logger.info(f"🟢 [USER INPUT]: {user_input}")

    if not user_input:
        state['parsed_intent'] = 'chat'
        state['system_message'] = "행동을 입력해주세요."
        return state

    # 시스템적 선택 처리
    if state.get('last_user_choice_idx', -1) != -1:
        state['parsed_intent'] = 'transition'
        return state

    scenario_id = state['scenario_id']
    curr_scene_id = state['current_scene_id']
    scenes = {s['scene_id']: s for s in get_scenario_by_id(scenario_id).get('scenes', [])}

    curr_scene = scenes.get(curr_scene_id)
    if not curr_scene:
        state['parsed_intent'] = 'chat'
        return state

    # 엔딩 체크
    endings = {e['ending_id']: e for e in get_scenario_by_id(scenario_id).get('endings', [])}
    if curr_scene_id in endings:
        state['parsed_intent'] = 'ending'
        return state

    transitions = curr_scene.get('transitions', [])
    scene_type = curr_scene.get('type', 'normal')
    scene_title = curr_scene.get('title', 'Untitled')
    npc_names = curr_scene.get('npcs', [])
    enemy_names = curr_scene.get('enemies', [])

    # =============================================================================
    # [1단계 API 호출] LLM을 통한 의도 분류
    # =============================================================================

    try:
        # transitions 목록을 문자열로 포맷팅 - 강조된 섹션으로 변경
        transitions_list = ""
        if transitions:
            transitions_list += "📋 **[AVAILABLE ACTIONS - 이것들이 다음 장면으로 이동 가능한 정답입니다]**\n"
            transitions_list += "다음 키워드들 중 하나와 유사한 입력이 들어오면 transition으로 분류하세요:\n\n"
            for idx, trans in enumerate(transitions):
                trigger = trans.get('trigger', '').strip()
                target = trans.get('target_scene_id', '')
                transitions_list += f"  {idx}. 트리거: \"{trigger}\" → {target}\n"
            transitions_list += "\n⚠️ 유저 입력이 위 트리거와 70% 이상 의미적으로 유사하면 transition으로 분류하세요."
        else:
            transitions_list = "없음 (이동 불가)"

        # YAML에서 intent_classifier 프롬프트 로드
        prompts = load_player_prompts()
        intent_classifier_template = prompts.get('intent_classifier', '')

        if not intent_classifier_template:
            # 프롬프트 로드 실패 시 기존 Fast-Track 방식 사용
            logger.warning("⚠️ intent_classifier prompt not found, falling back to fast-track")
            return _fast_track_intent_parser(state, user_input, curr_scene, scenario, endings)

        # 프롬프트 생성
        scenario = get_scenario_by_id(scenario_id)
        player_status = format_player_status(scenario, state.get('player_vars', {}))

        intent_prompt = intent_classifier_template.format(
            player_status=player_status,
            scene_title=scene_title,
            scene_type=scene_type,
            npc_list=', '.join(npc_names) if npc_names else '없음',
            enemy_list=', '.join(enemy_names) if enemy_names else '없음',
            transitions_list=transitions_list,
            user_input=user_input
        )

        # LLM 호출 (non-streaming)
        api_key = os.getenv("OPENROUTER_API_KEY")
        model_name = state.get('model', 'openai/tngtech/deepseek-r1t2-chimera:free')
        llm = get_cached_llm(api_key=api_key, model_name=model_name, streaming=False)

        response = llm.invoke(intent_prompt).content.strip()
        logger.info(f"🤖 [INTENT CLASSIFIER] Raw response: {response}")

        # JSON 파싱 시도
        # JSON이 마크다운 코드블록에 싸여있을 수 있으므로 추출
        json_match = re.search(r'\{.*\}', response, re.DOTALL)
        if json_match:
            json_str = json_match.group(0)
            intent_result = json.loads(json_str)

            intent_type = intent_result.get('intent', 'chat')
            transition_index = intent_result.get('transition_index', -1)
            confidence = intent_result.get('confidence', 0.0)
            reasoning = intent_result.get('reasoning', '')

            logger.info(f"🎯 [INTENT] Type: {intent_type}, Confidence: {confidence:.2f}, Reasoning: {reasoning}")

            # 의도에 따른 처리
            if intent_type == 'transition' and 0 <= transition_index < len(transitions):
                # 전투 씬에서 엔딩으로 가는 transition은 승리 조건 체크
                target_trans = transitions[transition_index]
                target = target_trans.get('target_scene_id', '').lower()
                is_ending_transition = target.startswith('ending') or target in endings

                if scene_type == 'battle' and is_ending_transition:
                    if not check_victory_condition(user_input, scenario, curr_scene):
                        logger.info(f"⚔️ [BATTLE] Transition blocked - victory condition not met")
                        state['parsed_intent'] = 'attack'
                        state['_internal_flags'] = state.get('_internal_flags', {})
                        state['_internal_flags']['battle_attack'] = True
                        return state

                state['last_user_choice_idx'] = transition_index
                state['parsed_intent'] = 'transition'
                return state

            elif intent_type == 'investigate':
                state['parsed_intent'] = 'investigate'
                return state

            elif intent_type == 'attack':
                # 승리 조건 확인
                if scene_type == 'battle' and not check_victory_condition(user_input, scenario, curr_scene):
                    state['parsed_intent'] = 'attack'
                    state['_internal_flags'] = state.get('_internal_flags', {})
                    state['_internal_flags']['battle_attack'] = True
                    return state
                else:
                    # 승리 조건 충족 시 transition으로 처리
                    state['parsed_intent'] = 'transition'
                    return state

            elif intent_type == 'defend':
                state['parsed_intent'] = 'defend'
                return state

            else:  # chat
                state['parsed_intent'] = 'chat'
                return state

        else:
            # JSON 파싱 실패 시 폴백
            logger.warning("⚠️ Failed to parse JSON from intent classifier, falling back to fast-track")
            return _fast_track_intent_parser(state, user_input, curr_scene, scenario, endings)

    except Exception as e:
        logger.error(f"❌ [INTENT CLASSIFIER] Error: {e}, falling back to fast-track")
        return _fast_track_intent_parser(state, user_input, curr_scene, scenario, endings)


def _fast_track_intent_parser(state: PlayerState, user_input: str, curr_scene: Dict, scenario: Dict, endings: Dict):
    """기존 Fast-Track 의도 파서 (폴백용)"""
    norm_input = normalize_text(user_input)
    transitions = curr_scene.get('transitions', [])
    scene_type = curr_scene.get('type', 'normal')

    if not transitions:
        state['parsed_intent'] = 'chat'
        return state

    # 공격 행동 감지
    attack_keywords = ['공격', '때리', '치', '베', '찌르', '쏘', '던지', '싸우', 'attack', 'hit', 'strike', 'fight', 'kill', '처치', '죽이', '무찌']
    is_attack_action = any(kw in user_input.lower() for kw in attack_keywords)

    if scene_type == 'battle' and is_attack_action:
        if not check_victory_condition(user_input, scenario, curr_scene):
            logger.info(f"⚔️ [BATTLE] Attack detected but victory condition not met. Continuing battle.")
            state['parsed_intent'] = 'attack'
            state['_internal_flags'] = state.get('_internal_flags', {})
            state['_internal_flags']['battle_attack'] = True
            return state

    # Fast-Track 매칭
    best_idx = -1
    highest_ratio = 0.0
    best_trigger_text = ""

    for idx, trans in enumerate(transitions):
        trigger = trans.get('trigger', '').strip()
        if not trigger: continue
        norm_trigger = normalize_text(trigger)
        target = trans.get('target_scene_id', '').lower()
        is_ending_transition = target.startswith('ending') or target in endings

        # 완전 포함 관계
        if norm_input in norm_trigger or norm_trigger in norm_input:
            if len(norm_input) >= 2:
                if scene_type == 'battle' and is_ending_transition:
                    if not check_victory_condition(user_input, scenario, curr_scene):
                        continue

                logger.info(f"⚡ [FAST-TRACK] Direct Match: '{user_input}' matched '{trigger}'")
                state['last_user_choice_idx'] = idx
                state['parsed_intent'] = 'transition'
                return state

        # 유사도 계산
        similarity = difflib.SequenceMatcher(None, norm_input, norm_trigger).ratio()

        if scene_type == 'battle' and is_ending_transition:
            if similarity < 0.8:
                continue

        if similarity > highest_ratio:
            highest_ratio = similarity
            best_idx = idx
            best_trigger_text = trigger

    # 0.6 이상: 성공
    if highest_ratio >= 0.6:
        target_trans = transitions[best_idx]
        target = target_trans.get('target_scene_id', '').lower()
        is_ending_transition = target.startswith('ending') or target in endings

        if scene_type == 'battle' and is_ending_transition:
            if not check_victory_condition(user_input, scenario, curr_scene):
                logger.info(f"⚔️ [BATTLE] Fuzzy match to ending blocked - victory condition not met")
                state['parsed_intent'] = 'attack'
                state['_internal_flags'] = state.get('_internal_flags', {})
                state['_internal_flags']['battle_attack'] = True
                return state

        logger.info(f"⚡ [FAST-TRACK] Fuzzy Match ({highest_ratio:.2f}): '{user_input}' -> '{best_trigger_text}'")
        state['last_user_choice_idx'] = best_idx
        state['parsed_intent'] = 'transition'
        return state

    # 0.4 ~ 0.59: Near Miss
    elif highest_ratio >= 0.4:
        logger.info(f"⚡ [FAST-TRACK] Near Miss ({highest_ratio:.2f}): '{user_input}' vs '{best_trigger_text}'")
        state['near_miss_trigger'] = best_trigger_text
        state['parsed_intent'] = 'chat'
        return state

    # 매칭 실패 -> 일반 채팅/힌트
    state['parsed_intent'] = 'chat'
    return state


def rule_node(state: PlayerState):
    """규칙 엔진 (이동 및 상태 변경) - WorldState 통합"""
    idx = state['last_user_choice_idx']
    scenario_id = state['scenario_id']
    curr_scene_id = state['current_scene_id']
    prev_scene_id = state.get('previous_scene_id')

    all_scenes = {s['scene_id']: s for s in get_scenario_by_id(scenario_id)['scenes']}
    all_endings = {e['ending_id']: e for e in get_scenario_by_id(scenario_id).get('endings', [])}

    sys_msg = []
    curr_scene = all_scenes.get(curr_scene_id)
    transitions = curr_scene.get('transitions', []) if curr_scene else []

    # WorldState 인스턴스 가져오기 및 복원
    world_state = WorldState()

    # [FIX] 기존 world_state가 있으면 복원
    if 'world_state' in state and state['world_state']:
        world_state.from_dict(state['world_state'])
    else:
        # 처음 생성하는 경우 시나리오로 초기화
        scenario = get_scenario_by_id(scenario_id)
        world_state.initialize_from_scenario(scenario)

    # ✅ 작업 2: stuck_count 초기화 (state에 없으면 0으로 설정)
    if 'stuck_count' not in state:
        state['stuck_count'] = 0
        logger.info(f"🔧 [STUCK_COUNT] Initialized to 0")

    # 🔴 장면 전환 시도 전 현재 씬을 정확히 캡처 (world_state.location 우선)
    scene_before_transition = world_state.location or state.get('current_scene_id', '')
    user_action = state.get('last_user_input', '').strip()
    logger.info(f"🎬 [APPLY_EFFECTS] Scene before transition: {scene_before_transition}, Intent: {state['parsed_intent']}, Transition index: {idx}")

    if state['parsed_intent'] == 'transition' and 0 <= idx < len(transitions):
        trans = transitions[idx]
        effects = trans.get('effects', [])
        next_id = trans.get('target_scene_id')
        trigger_used = trans.get('trigger', 'unknown')

        logger.info(f"🎯 [TRANSITION] Attempting transition to: {next_id}")

        # ✅ 효과 적용을 WorldState로 일원화
        if effects:
            world_state.update_state(effects)
            # 효과가 player_vars에도 반영되도록 동기화
            for eff in effects:
                if isinstance(eff, dict):
                    key = eff.get("target", "").lower()
                    operation = eff.get("operation", "add")
                    raw_val = eff.get("value", 0)

                    # 아이템 효과
                    if operation in ["gain_item", "lose_item"]:
                        item_name = str(raw_val)
                        inventory = state['player_vars'].get('inventory', [])
                        if not isinstance(inventory, list):
                            inventory = []

                        if operation == "gain_item":
                            if item_name not in inventory:
                                inventory.append(item_name)
                            sys_msg.append(f"📦 획득: {item_name}")
                        elif operation == "lose_item":
                            if item_name in inventory:
                                inventory.remove(item_name)
                            sys_msg.append(f"🗑️ 사용: {item_name}")

                        state['player_vars']['inventory'] = inventory
                        continue

                    # 수치 효과
                    val = 0
                    if isinstance(raw_val, (int, float)):
                        val = int(raw_val)
                    elif isinstance(raw_val, str):
                        if raw_val.isdigit() or (raw_val.startswith('-') and raw_val[1:].isdigit()):
                            val = int(raw_val)

                    if key:
                        current_val = state['player_vars'].get(key, 0)
                        if not isinstance(current_val, (int, float)):
                            current_val = 0

                        if operation == "add":
                            new_val = current_val + val
                            if val > 0:
                                sys_msg.append(f"{key.upper()} +{val}")
                            else:
                                sys_msg.append(f"{key.upper()} {val}")
                        elif operation == "subtract":
                            new_val = max(0, current_val - abs(val))
                            sys_msg.append(f"{key.upper()} -{abs(val)}")
                        elif operation == "set":
                            new_val = val
                            sys_msg.append(f"{key.upper()} = {val}")
                        else:
                            new_val = current_val

                        state['player_vars'][key] = new_val

        # 씬 이동
        if next_id:
            # 🔴 이동 전 현재 위치를 scene_before_transition에서 가져오기
            from_scene = scene_before_transition

            state['current_scene_id'] = next_id
            world_state.location = next_id

            # ✅ 작업 3: 장면 전환 성공 시 서사 이벤트 기록 (이동 이유 포함)
            world_state.add_narrative_event(
                f"유저가 '{trigger_used}'을(를) 통해 [{from_scene}]에서 [{next_id}]로 이동함"
            )

            # ✅ 작업 2: 장면 전환 성공 시 stuck_count 초기화
            old_stuck_count = state.get('stuck_count', 0)
            state['stuck_count'] = 0
            logger.info(f"✅ [MOVE SUCCESS] {from_scene} -> {next_id} | stuck_count: {old_stuck_count} -> 0")
        else:
            # target_scene_id가 없는 경우 (비정상)
            state['stuck_count'] = state.get('stuck_count', 0) + 1
            logger.warning(f"⚠️ [TRANSITION FAILED] No target_scene_id | stuck_count: {state['stuck_count']}")

            # ✅ 작업 3: 장면 전환 실패 시 서사 기록
            if user_action:
                world_state.add_narrative_event(
                    f"유저가 '{user_action[:30]}...'을(를) 시도했으나 아무 일도 일어나지 않음"
                )
    else:
        # ✅ 작업 3: 장면 전환 실패 (씬 유지) 시 stuck_count 증가 및 서사 기록
        if user_action:
            old_stuck_count = state.get('stuck_count', 0)
            state['stuck_count'] = old_stuck_count + 1
            logger.info(f"🔄 [STUCK] Player stuck in scene '{scene_before_transition}' | Intent: {state['parsed_intent']} | stuck_count: {old_stuck_count} -> {state['stuck_count']}")

            # 서사 이벤트 기록
            world_state.add_narrative_event(
                f"유저가 '{user_action[:30]}...'을(를) 시도했으나 장면 전환 없이 현재 위치에 머뭄"
            )
        else:
            logger.debug(f"⏸️ [NO INPUT] No user input, stuck_count unchanged: {state.get('stuck_count', 0)}")

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

    # ✅ 작업 2: WorldState 스냅샷 저장 (stuck_count는 state에 저장됨)
    state['world_state'] = world_state.to_dict()

    return state


def npc_node(state: PlayerState):
    """NPC 대화 (이동 아닐 때만 발동)"""
    if state.get('parsed_intent') != 'chat':
        state['npc_output'] = ""
        return state

    # [추가] stuck_count 초기화 (state에 없으면 0으로 설정)
    if 'stuck_count' not in state:
        state['stuck_count'] = 0
        logger.info(f"🔧 [STUCK_COUNT] Initialized to 0 in npc_node")

    # WorldState 인스턴스 가져오기 및 복원
    scenario_id = state['scenario_id']
    world_state = WorldState()
    if 'world_state' in state and state['world_state']:
        world_state.from_dict(state['world_state'])

    # [추가] 장면 전환 실패 (씬 유지) 시 stuck_count 증가
    curr_scene_id = state.get('current_scene_id', '')
    prev_scene_id = state.get('previous_scene_id', '')

    if state.get('last_user_input', '').strip():
        old_stuck_count = state.get('stuck_count', 0)
        state['stuck_count'] = old_stuck_count + 1
        logger.info(f"🔄 [STUCK] Player stuck in scene '{curr_scene_id}' | Intent: chat | stuck_count: {old_stuck_count} -> {state['stuck_count']}")

    curr_id = state['current_scene_id']
    all_scenes = {s['scene_id']: s for s in get_scenario_by_id(scenario_id)['scenes']}
    curr_scene = all_scenes.get(curr_id)
    npc_names = curr_scene.get('npcs', []) if curr_scene else []

    user_input = state['last_user_input']

    # [추가] 인벤토리 검증: 아이템 사용 시도 감지
    item_keywords = ['사용', '쓴다', '쏜다', '던진다', '먹는다', '마신다', '착용', '장착', '입는다',
                     'use', 'shoot', 'throw', 'eat', 'drink', 'wear', '뿌린다', '흔든다', '꺼낸다']

    if any(keyword in user_input.lower() for keyword in item_keywords):
        player_inventory = state.get('player_vars', {}).get('inventory', [])
        has_item = False

        for item in player_inventory:
            if item.lower() in user_input.lower():
                has_item = True
                break

        if not has_item:
            rejection_messages = [
                "주머니를 더듬어 보았지만 찾을 수 없습니다.",
                "소지품을 확인해보니 그것은 가지고 있지 않습니다.",
                "당신은 그 물건을 가지고 있지 않습니다.",
                "손을 뻗었지만 허공만 움켜쥐게 됩니다. 그것은 당신에게 없는 것입니다."
            ]
            state['npc_output'] = random.choice(rejection_messages)
            logger.info(f"🚫 [INVENTORY CHECK] Item not found in inventory. User input: {user_input}")
            return state

    # 기존 NPC 대화 로직
    if not npc_names:
        state['npc_output'] = ""
        return state

    target_npc_name = npc_names[0]
    npc_info = {"name": target_npc_name, "role": "Unknown", "personality": "보통"}

    for npc in get_scenario_by_id(scenario_id).get('npcs', []):
        if npc.get('name') == target_npc_name:
            npc_info['role'] = npc.get('role', 'Unknown')
            npc_info['personality'] = npc.get('personality', '보통')
            npc_info['dialogue_style'] = npc.get('dialogue_style', '')
            break

    history = state.get('history', [])
    history_context = "\n".join(history[-3:]) if history else "대화 시작"

    # [추가] 현재 장면의 transitions_hints와 stuck_level 추출
    transitions_list = []
    if curr_scene:
        for t in curr_scene.get('transitions', []):
            trigger = t.get('trigger', '알 수 없음')
            transitions_list.append(trigger)

    transitions_hints = ", ".join(transitions_list) if transitions_list else "힌트 없음"
    stuck_level = state.get('stuck_count', 0)

    # YAML에서 프롬프트 로드
    prompts = load_player_prompts()
    prompt_template = prompts.get('npc_dialogue', '')

    # ✅ WorldState 컨텍스트 추가
    world_context = world_state.get_llm_context()

    if prompt_template:
        scenario = get_scenario_by_id(scenario_id)
        player_status = format_player_status(scenario, state.get('player_vars', {}))

        # [수정] WorldState 컨텍스트를 프롬프트에 포함
        prompt = f"""{world_context}

{prompt_template.format(
            player_status=player_status,
            npc_name=npc_info['name'],
            npc_role=npc_info['role'],
            npc_personality=npc_info['personality'],
            history_context=history_context,
            user_input=user_input,
            transitions_hints=transitions_hints,
            stuck_level=stuck_level
        )}"""
    else:
        # 폴백 프롬프트 (YAML 로드 실패 시)
        logger.warning("⚠️ Failed to load npc_dialogue from YAML, using fallback")
        prompt = f"""{world_context}

당신은 텍스트 RPG의 NPC입니다.
이름: {npc_info['name']}, 역할: {npc_info['role']}, 성격: {npc_info['personality']}
플레이어: "{user_input}"
NPC로서 1-2문장으로 응답하세요."""

    try:
        api_key = os.getenv("OPENROUTER_API_KEY")
        model_name = state.get('model', 'openai/tngtech/deepseek-r1t2-chimera:free')
        llm = get_cached_llm(api_key=api_key, model_name=model_name, streaming=False)
        response = llm.invoke(prompt).content.strip()

        # [추가] 응답 검증 - 사용자 입력을 그대로 반복하는 경우 LLM으로 재생성
        normalized_input = user_input.lower().replace(" ", "")
        normalized_response = response.lower().replace(" ", "")

        if normalized_input in normalized_response and len(normalized_response) < len(normalized_input) + 10:
            # 사용자 입력을 단순 반복한 경우 폴백 프롬프트로 재시도
            logger.warning(f"⚠️ NPC response too similar to user input, retrying with fallback prompt")
            fallback_template = prompts.get('npc_fallback', '')
            if fallback_template:
                fallback_prompt = fallback_template.format(
                    npc_name=npc_info['name'],
                    npc_role=npc_info['role'],
                    user_input=user_input
                )
                response = llm.invoke(fallback_prompt).content.strip()

        state['npc_output'] = response

        # ✅ 작업 2: NPC 대화 서사 요약 및 기록 - LLM을 활용하여 대화 핵심 내용 요약
        try:
            # 대화 요약 프롬프트 생성
            summary_prompt = f"""다음 대화를 한 문장으로 간결하게 요약하세요:
플레이어: "{user_input}"
NPC ({target_npc_name}): "{response}"

요약 형식: "플레이어가 [NPC]에게 [행동/요청]했고, NPC는 [반응]함"
예시: "플레이어가 노인 J에게 술집을 불태우겠다고 협박하며 지도를 요구했고, 노인은 겁에 질려 반응함"

요약:"""

            summary_llm = get_cached_llm(api_key=api_key, model_name=model_name, streaming=False)
            conversation_summary = summary_llm.invoke(summary_prompt).content.strip()

            # 요약이 너무 길면 잘라내기
            if len(conversation_summary) > 100:
                conversation_summary = conversation_summary[:97] + "..."

            world_state.add_narrative_event(conversation_summary)
            logger.info(f"📖 [NPC DIALOGUE] Summary added to narrative: {conversation_summary}")

        except Exception as summary_error:
            # 요약 실패 시 간단한 템플릿 사용
            logger.warning(f"⚠️ Failed to generate conversation summary: {summary_error}")
            fallback_summary = f"플레이어가 '{target_npc_name}'와 대화함 (주제: {user_input[:20]}...)"
            world_state.add_narrative_event(fallback_summary)

        if 'history' not in state: state['history'] = []
        state['history'].append(f"User: {user_input}")
        state['history'].append(f"NPC({target_npc_name}): {response}")

        logger.info(f"💬 [NPC] {target_npc_name}: {response}")

    except Exception as e:
        logger.error(f"NPC generation error: {e}")
        # 에러 시에도 LLM으로 간단한 응답 생성 시도
        try:
            fallback_template = prompts.get('npc_fallback', '')
            if fallback_template:
                fallback_prompt = fallback_template.format(
                    npc_name=npc_info['name'],
                    npc_role=npc_info['role'],
                    user_input=user_input
                )
                api_key = os.getenv("OPENROUTER_API_KEY")
                llm = get_cached_llm(api_key=api_key, model_name='openai/gpt-3.5-turbo', streaming=False)
                state['npc_output'] = llm.invoke(fallback_prompt).content.strip()
            else:
                state['npc_output'] = ""
        except Exception:
            state['npc_output'] = ""

    # WorldState 스냅샷 저장
    state['world_state'] = world_state.to_dict()

    return state


def check_npc_appearance(state: PlayerState) -> str:
    """NPC 및 적 등장 (LLM 기반 생성)"""
    scenario_id = state['scenario_id']
    curr_id = state['current_scene_id']

    # 씬 변경 없으면 등장 메시지 생략
    if state.get('previous_scene_id') == curr_id:
        return ""

    all_scenes = {s['scene_id']: s for s in get_scenario_by_id(scenario_id)['scenes']}
    curr_scene = all_scenes.get(curr_id)
    if not curr_scene: return ""

    # [FIX] NPC와 적을 모두 처리
    npc_names = curr_scene.get('npcs', [])
    enemy_names = curr_scene.get('enemies', [])
    scene_type = curr_scene.get('type', 'normal')
    scene_title = curr_scene.get('title', 'Untitled')

    if not npc_names and not enemy_names: return ""

    scene_history_key = f"npc_appeared_{curr_id}"
    player_vars = state.get('player_vars', {})
    if player_vars.get(scene_history_key): return ""

    state['player_vars'][scene_history_key] = True
    introductions = []

    # YAML에서 프롬프트 로드
    prompts = load_player_prompts()
    api_key = os.getenv("OPENROUTER_API_KEY")
    model_name = state.get('model', 'openai/tngtech/deepseek-r1t2-chimera:free')

    # [FIX] 장면 유형에 따른 메시지 - LLM으로 생성
    if scene_type == 'battle':
        battle_start_template = prompts.get('battle_start', '')
        if battle_start_template:
            battle_start_prompt = battle_start_template.format(
                scene_title=scene_title,
                enemy_names=', '.join(enemy_names) if enemy_names else '알 수 없는 적'
            )
            try:
                llm = get_cached_llm(api_key=api_key, model_name=model_name, streaming=False)
                battle_msg = llm.invoke(battle_start_prompt).content.strip()
                introductions.append(f"""
                <div class='battle-alert text-red-400 font-bold my-3 p-3 bg-red-900/30 rounded border-2 border-red-500 animate-pulse'>
                    ⚔️ {battle_msg}
                </div>
                """)
            except Exception as e:
                logger.error(f"Battle start message generation error: {e}")
                introductions.append("""
                <div class='battle-alert text-red-400 font-bold my-3 p-3 bg-red-900/30 rounded border-2 border-red-500 animate-pulse'>
                    ⚔️ 전투가 시작됩니다!
                </div>
                """)

    # NPC 등장 - LLM으로 생성
    if npc_names:
        npc_appearance_template = prompts.get('npc_appearance', '')
        for npc_name in npc_names:
            # NPC 역할 찾기
            npc_role = "Unknown"
            for npc in get_scenario_by_id(scenario_id).get('npcs', []):
                if npc.get('name') == npc_name:
                    npc_role = npc.get('role', 'Unknown')
                    break

            if npc_appearance_template:
                npc_prompt = npc_appearance_template.format(
                    scene_title=scene_title,
                    npc_name=npc_name,
                    npc_role=npc_role
                )
                try:
                    llm = get_cached_llm(api_key=api_key, model_name=model_name, streaming=False)
                    npc_action = llm.invoke(npc_prompt).content.strip()
                    intro_html = f"""
                    <div class='npc-intro text-green-300 italic my-2 p-2 bg-green-900/20 rounded border-l-2 border-green-500'>
                        👀 {npc_action}
                    </div>
                    """
                    introductions.append(intro_html)
                except Exception as e:
                    logger.error(f"NPC appearance generation error: {e}")
                    intro_html = f"""
                    <div class='npc-intro text-green-300 italic my-2 p-2 bg-green-900/20 rounded border-l-2 border-green-500'>
                        👀 <span class='font-bold'>{npc_name}</span>이(가) 당신을 바라봅니다.
                    </div>
                    """
                    introductions.append(intro_html)
            else:
                intro_html = f"""
                <div class='npc-intro text-green-300 italic my-2 p-2 bg-green-900/20 rounded border-l-2 border-green-500'>
                    👀 <span class='font-bold'>{npc_name}</span>이(가) 당신을 바라봅니다.
                </div>
                """
                introductions.append(intro_html)

    # [FIX] 적 등장 처리 - LLM으로 생성
    if enemy_names:
        enemy_appearance_template = prompts.get('enemy_appearance', '')
        for enemy_name in enemy_names:
            if enemy_appearance_template:
                enemy_prompt = enemy_appearance_template.format(
                    scene_title=scene_title,
                    enemy_name=enemy_name
                )
                try:
                    llm = get_cached_llm(api_key=api_key, model_name=model_name, streaming=False)
                    enemy_action = llm.invoke(enemy_prompt).content.strip()
                    intro_html = f"""
                    <div class='enemy-intro text-red-400 font-bold my-2 p-2 bg-red-900/30 rounded border-l-2 border-red-500'>
                        ⚔️ {enemy_action}
                    </div>
                    """
                    introductions.append(intro_html)
                except Exception as e:
                    logger.error(f"Enemy appearance generation error: {e}")
                    intro_html = f"""
                    <div class='enemy-intro text-red-400 font-bold my-2 p-2 bg-red-900/30 rounded border-l-2 border-red-500'>
                        ⚔️ <span class='font-bold'>{enemy_name}</span>이(가) 나타났습니다!
                    </div>
                    """
                    introductions.append(intro_html)
            else:
                intro_html = f"""
                <div class='enemy-intro text-red-400 font-bold my-2 p-2 bg-red-900/30 rounded border-l-2 border-red-500'>
                    ⚔️ <span class='font-bold'>{enemy_name}</span>이(가) 나타났습니다!
                </div>
                """
                introductions.append(intro_html)

    return "\n".join(introductions)


def narrator_node(state: PlayerState):
    """
    내레이션 노드 - 모든 액션의 마지막에 실행됨
    턴 증가 로직을 여기서 처리 (게임 시작이 아닐 때만)
    """
    # WorldState 인스턴스 가져오기 및 복원
    scenario_id = state.get('scenario_id')
    world_state = WorldState()

    # 기존 world_state가 있으면 복원
    if 'world_state' in state and state['world_state']:
        world_state.from_dict(state['world_state'])
    else:
        # 처음 생성하는 경우 시나리오로 초기화
        scenario = get_scenario_by_id(scenario_id)
        world_state.initialize_from_scenario(scenario)

    # 턴 증가 (게임 시작이 아닐 때만)
    is_game_start = state.get('is_game_start', False)
    if not is_game_start:
        world_state.increment_turn()
        logger.info(f"⏱️ [TURN] Turn count increased to {world_state.turn_count}")
    else:
        logger.info(f"⏱️ [TURN] Game start - turn count not increased (current: {world_state.turn_count})")

    # WorldState 스냅샷 저장
    state['world_state'] = world_state.to_dict()

    return state


# --- Streaming Generators (SSE) ---

def prologue_stream_generator(state: PlayerState):
    # [FIX] scenario_id로 시나리오 조회
    scenario_id = state.get('scenario_id')
    if not scenario_id:
        yield "이야기가 시작됩니다..."
        return

    scenario = get_scenario_by_id(scenario_id)
    if not scenario:
        yield "이야기가 시작됩니다..."
        return

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
    [2단계 API 호출 구조 - 2단계: 서사 생성]
    1단계에서 분류된 의도(parsed_intent)에 따라 전용 서사 프롬프트를 선택하여 스트리밍

    나레이션 모드:
    [MODE 1] 씬 유지 + 의도별 분기 (investigate/attack/defend/chat/near_miss)
    [MODE 2] 씬 변경 -> 장면 묘사
    """
    scenario_id = state['scenario_id']
    curr_id = state['current_scene_id']
    prev_id = state.get('previous_scene_id')
    user_input = state.get('last_user_input', '')
    parsed_intent = state.get('parsed_intent', 'chat')

    scenario = get_scenario_by_id(scenario_id)
    all_scenes = {s['scene_id']: s for s in scenario['scenes']}
    all_endings = {e['ending_id']: e for e in scenario.get('endings', [])}

    # WorldState 인스턴스 가져오기
    world_state = WorldState()
    if 'world_state' in state and state['world_state']:
        world_state.from_dict(state['world_state'])

    # [추가] current_scene_id가 'prologue'이거나 존재하지 않는 경우 폴백 처리
    if curr_id == 'prologue' or curr_id not in all_scenes:
        logger.warning(f"⚠️ Scene not found or is prologue: {curr_id}")

        # start_scene_id로 폴백
        start_scene_id = scenario.get('start_scene_id')
        if not start_scene_id or start_scene_id not in all_scenes:
            # start_scene_id도 없으면 첫 번째 씬 사용
            scenes_list = scenario.get('scenes', [])
            if scenes_list:
                start_scene_id = scenes_list[0].get('scene_id', 'Scene-1')
            else:
                start_scene_id = 'Scene-1'

        logger.info(f"🔧 [SCENE FALLBACK] {curr_id} -> {start_scene_id}")
        state['current_scene_id'] = start_scene_id
        world_state.location = start_scene_id
        curr_id = start_scene_id

        # [추가] 폴백 후 다시 all_scenes에서 확인
        if curr_id not in all_scenes:
            logger.error(f"❌ [CRITICAL] Even after fallback, scene not found: {curr_id}")
            # 재시도 로직
            if retry_count < max_retries:
                yield f"__RETRY_SIGNAL__"
                return
            fallback_msg = get_narrative_fallback_message(scenario)
            yield f"""
            <div class="bg-yellow-900/30 border border-yellow-700/50 rounded-lg p-4 my-2">
                <div class="text-yellow-400 serif-font">{fallback_msg}</div>
            </div>
            """
            return

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
        logger.warning(f"❌ Scene not found after fallback: {curr_id}")
        if retry_count < max_retries:
            yield f"__RETRY_SIGNAL__"
            return
        fallback_msg = get_narrative_fallback_message(scenario)
        yield f"""
        <div class="bg-yellow-900/30 border border-yellow-700/50 rounded-lg p-4 my-2">
            <div class="text-yellow-400 serif-font">{fallback_msg}</div>
        </div>
        """
        # 최후의 수단: start_scene_id로 강제 이동
        start_scene_id = scenario.get('start_scene_id')
        if start_scene_id and start_scene_id in all_scenes:
            state['current_scene_id'] = start_scene_id
            world_state.location = start_scene_id
        elif scenario.get('scenes'):
            fallback_scene_id = scenario['scenes'][0].get('scene_id', 'Scene-1')
            state['current_scene_id'] = fallback_scene_id
            world_state.location = fallback_scene_id
        return

    scene_title = curr_scene.get('title', 'Untitled')
    scene_type = curr_scene.get('type', 'normal')
    enemy_names = curr_scene.get('enemies', [])
    npc_names = curr_scene.get('npcs', [])

    # =============================================================================
    # [MODE 1] 씬 유지됨 -> 의도(parsed_intent)에 따른 전용 서사 프롬프트 선택
    # =============================================================================
    if prev_id == curr_id and user_input:
        prompts = load_player_prompts()
        weakness_hint = get_npc_weakness_hint(scenario, enemy_names) or "주변을 살펴보니 활용할 수 있는 것이 보입니다."

        # [2단계] parsed_intent에 따라 전용 프롬프트 선택
        prompt_template = None
        prompt_key = None

        if parsed_intent == 'investigate':
            # 조사/탐색 행동
            prompt_key = 'battle_investigation' if scene_type == 'battle' else 'battle_investigation'
            prompt_template = prompts.get(prompt_key, '')
            if prompt_template:
                narrative_prompt = prompt_template.format(
                    user_input=user_input,
                    scene_title=scene_title,
                    weakness_hint=weakness_hint if weakness_hint else "주변을 살펴보니 특이한 물건이 눈에 띕니다."
                )

        elif parsed_intent == 'attack':
            # 공격 행동 (승리 조건 미충족)
            prompt_key = 'battle_attack_result'
            prompt_template = prompts.get(prompt_key, '')
            if prompt_template:
                narrative_prompt = prompt_template.format(
                    user_input=user_input,
                    scene_title=scene_title,
                    weakness_hint=weakness_hint
                )

        elif parsed_intent == 'defend':
            # 방어 행동
            prompt_key = 'battle_defense'
            prompt_template = prompts.get(prompt_key, '')
            if prompt_template:
                narrative_prompt = prompt_template.format(
                    user_input=user_input,
                    scene_title=scene_title,
                    weakness_hint=weakness_hint
                )

        # Near Miss 처리
        near_miss = state.get('near_miss_trigger')
        if near_miss and parsed_intent == 'chat':
            prompt_key = 'near_miss'
            prompt_template = prompts.get(prompt_key, '')
            if prompt_template:
                player_status = format_player_status(scenario, state.get('player_vars', {}))

                narrative_prompt = prompt_template.format(
                    user_input=user_input,
                    player_status=player_status,
                    near_miss_trigger=near_miss
                )

        # 의도별 프롬프트가 설정되었으면 LLM 스트리밍
        if prompt_template and 'narrative_prompt' in locals():
            try:
                api_key = os.getenv("OPENROUTER_API_KEY")
                model_name = state.get('model', 'openai/tngtech/deepseek-r1t2-chimera:free')
                llm = get_cached_llm(api_key=api_key, model_name=model_name, streaming=True)

                logger.info(f"🎬 [NARRATIVE] Using prompt: {prompt_key} for intent: {parsed_intent}")

                for chunk in llm.stream(narrative_prompt):
                    if chunk.content:
                        yield chunk.content
                return

            except Exception as e:
                logger.error(f"Narrative generation error for intent '{parsed_intent}': {e}")
                # 폴백 메시지
                if parsed_intent == 'investigate':
                    if weakness_hint:
                        yield f"주변을 살핍니다. {weakness_hint}"
                    else:
                        yield "주변을 둘러보니 활용할 수 있는 것들이 보입니다."
                    return
                elif parsed_intent == 'attack':
                    yield random.choice(get_battle_attack_messages())
                    return
                elif parsed_intent == 'defend':
                    yield random.choice(get_battle_defensive_messages())
                    return
                elif near_miss:
                    yield random.choice(get_near_miss_narrative_hints())
                    return

        # NPC 대화가 있으면 나레이션 스킵
        npc_output = state.get('npc_output', '')
        if npc_output:
            yield ""
            return

        # 전투 씬에서 일반 chat 행동 (프롬프트 없을 때)
        if scene_type == 'battle' and parsed_intent == 'chat':
            battle_continue_template = prompts.get('battle_continue', '')
            if battle_continue_template:
                battle_continue_prompt = battle_continue_template.format(
                    user_input=user_input,
                    scene_title=scene_title,
                    weakness_hint=weakness_hint
                )
                try:
                    api_key = os.getenv("OPENROUTER_API_KEY")
                    model_name = state.get('model', 'openai/tngtech/deepseek-r1t2-chimera:free')
                    llm = get_cached_llm(api_key=api_key, model_name=model_name, streaming=True)
                    for chunk in llm.stream(battle_continue_prompt):
                        if chunk.content: yield chunk.content
                except Exception:
                    yield random.choice(get_battle_stalemate_messages())
                return

        # 일반 씬에서 chat 행동 시 힌트 모드 (transitions 기반)
        if parsed_intent == 'chat' and not npc_output:
            transitions = curr_scene.get('transitions', [])
            filtered_transitions = filter_negative_transitions(transitions, scenario)

            if filtered_transitions:
                # transitions_hints 생성
                transitions_hints = "\n".join([f"- {t.get('trigger', '')}" for t in filtered_transitions])

                hint_mode_template = prompts.get('hint_mode', '')
                if hint_mode_template:
                    player_status = format_player_status(scenario, state.get('player_vars', {}))

                    # [추가] stuck_count를 stuck_level로 전달
                    stuck_level = state.get('stuck_count', 0)

                    hint_prompt = hint_mode_template.format(
                        user_input=user_input,
                        player_status=player_status,
                        scene_title=scene_title,
                        transitions_hints=transitions_hints,
                        stuck_level=stuck_level
                    )
                    try:
                        api_key = os.getenv("OPENROUTER_API_KEY")
                        model_name = state.get('model', 'openai/tngtech/deepseek-r1t2-chimera:free')
                        llm = get_cached_llm(api_key=api_key, model_name=model_name, streaming=True)

                        logger.info(f"💡 [HINT MODE] stuck_level: {stuck_level}")

                        for chunk in llm.stream(hint_prompt):
                            if chunk.content: yield chunk.content
                        return
                    except Exception as e:
                        logger.error(f"Hint mode generation error: {e}")
                        # 폴백
                        yield "주변을 둘러보니 여러 가지 시도해볼 수 있을 것 같습니다."
                        return

            # transitions가 없으면 일반 메시지
            yield "당신은 잠시 주변을 살핍니다."
            return

    # =============================================================================
    # [MODE 2] 씬 변경됨 -> 장면 묘사
    # =============================================================================
    scene_desc = curr_scene.get('description', '')

    npc_intro = check_npc_appearance(state)
    if npc_intro: yield npc_intro + "<br><br>"

    # YAML에서 씬 묘사 프롬프트 로드
    npc_list = ', '.join(npc_names) if npc_names else '없음'
    prompts = load_player_prompts()
    scene_prompt_template = prompts.get('scene_description', '')

    if scene_prompt_template:
        player_status = format_player_status(scenario, state.get('player_vars', {}))

        # [추가] transitions 리스트 생성 - 장면 묘사에 포함할 선택지들
        transitions = curr_scene.get('transitions', [])
        available_transitions = ""
        if transitions:
            # 부정적 엔딩으로 가는 transition 제외
            filtered_transitions = filter_negative_transitions(transitions, scenario)
            if filtered_transitions:
                available_transitions = "\n".join([f"- {t.get('trigger', '')}" for t in filtered_transitions])
            else:
                available_transitions = "현재 특별한 선택지가 없습니다."
        else:
            available_transitions = "현재 특별한 선택지가 없습니다."

        # 씬 변경 시 유저 입력 컨텍스트 포함
        if user_input:
            context_prefix = f"""**최우선 지침: 유저의 마지막 입력("{user_input}")이 이 장면으로의 전환을 일으켰습니다. 그 결과를 먼저 서술하세요.**

"""
            prompt = context_prefix + scene_prompt_template.format(
                player_status=player_status,
                scene_title=scene_title,
                scene_desc=scene_desc,
                npc_list=npc_list,
                available_transitions=available_transitions
            )
        else:
            prompt = scene_prompt_template.format(
                player_status=player_status,
                scene_title=scene_title,
                scene_desc=scene_desc,
                npc_list=npc_list,
                available_transitions=available_transitions
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
