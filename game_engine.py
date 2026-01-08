import random
import json
import logging
import os
import re
import difflib
from typing import TypedDict, List, Dict, Any
from langgraph.graph import StateGraph, END
from llm_factory import LLMFactory
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


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


def normalize_text(text: str) -> str:
    """텍스트 정규화 (공백 제거, 소문자)"""
    return text.lower().replace(" ", "")


# --- Nodes ---

def intent_parser_node(state: PlayerState):
    """
    [최적화됨] 의도 파서
    - LLM 호출 제거: 오직 파이썬 내부 연산(Fast-Track)만 수행하여 속도 극대화
    - 매칭 실패 시 -> 지체 없이 Chat/Hint 모드로 전환
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

    # 🚀 [SPEED-UP] Fast-Track 매칭
    best_idx = -1
    highest_ratio = 0.0
    best_trigger_text = ""  # 변수 초기화 (안전장치)

    for idx, trans in enumerate(transitions):
        trigger = trans.get('trigger', '').strip()
        if not trigger: continue
        norm_trigger = normalize_text(trigger)

        # 1. 완전 포함 관계 확인 (가장 확실함 -> 즉시 리턴 가능)
        if norm_input in norm_trigger or norm_trigger in norm_input:
            if len(norm_input) >= 2:
                logger.info(f"⚡ [FAST-TRACK] Direct Match: '{user_input}' matched '{trigger}'")
                state['last_user_choice_idx'] = idx
                state['parsed_intent'] = 'transition'
                return state

        # 2. 유사도 계산 (Best Match 찾기 위해 루프 돎)
        similarity = difflib.SequenceMatcher(None, norm_input, norm_trigger).ratio()
        if similarity > highest_ratio:
            highest_ratio = similarity
            best_idx = idx
            best_trigger_text = trigger

    # [수정] 루프 종료 후 '가장 높은 점수'로 최종 판단
    # 0.6 이상: 성공
    if highest_ratio >= 0.6:
        logger.info(f"⚡ [FAST-TRACK] Fuzzy Match ({highest_ratio:.2f}): '{user_input}' -> '{best_trigger_text}'")
        state['last_user_choice_idx'] = best_idx
        state['parsed_intent'] = 'transition'
        return state

    # 0.4 ~ 0.59: 아까운 실패 (Near Miss)
    elif highest_ratio >= 0.4:
        logger.info(f"⚡ [FAST-TRACK] Near Miss ({highest_ratio:.2f}): '{user_input}' vs '{best_trigger_text}'")
        state['near_miss_trigger'] = best_trigger_text
        state['parsed_intent'] = 'chat'  # 이동은 실패했지만 힌트 줄 예정
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
    # ... (기존 코드 동일) ...
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
    npc_info = f"Name: {target_npc_name}"
    for npc in scenario.get('npcs', []):
        if npc.get('name') == target_npc_name:
            npc_info += f"\nRole: {npc.get('role', 'Unknown')}\nPersonality: {npc.get('personality')}"
            break

    history = state.get('history', [])
    history_context = "\n".join(history[-3:]) if history else ""

    prompt = f"""
    [ROLE] Act as NPC '{target_npc_name}'. Scene: {curr_scene.get('title')}
    [PROFILE] {npc_info}
    [HISTORY] {history_context}
    [USER] "{state['last_user_input']}"
    [GOAL] Reply in Korean. Short (1 sentence). Natural tone.
    """

    try:
        api_key = os.getenv("OPENROUTER_API_KEY")
        # 상태에서 모델 가져오기 (없으면 기본값 사용)
        model_name = state.get('model', 'openai/tngtech/deepseek-r1t2-chimera:free')
        llm = LLMFactory.get_llm(api_key=api_key, model_name=model_name)
        response = llm.invoke(prompt).content.strip()
        state['npc_output'] = response

        if 'history' not in state: state['history'] = []
        state['history'].append(f"User: {state['last_user_input']}")
        state['history'].append(f"NPC({target_npc_name}): {response}")
    except Exception:
        state['npc_output'] = "..."

    return state


def check_npc_appearance(state: PlayerState) -> str:
    """NPC 등장 (템플릿 기반)"""
    # ... (기존 코드 동일) ...
    scenario = state['scenario']
    curr_id = state['current_scene_id']

    # 씬 변경 없으면 등장 메시지 생략
    if state.get('previous_scene_id') == curr_id:
        return ""

    all_scenes = {s['scene_id']: s for s in scenario['scenes']}
    curr_scene = all_scenes.get(curr_id)
    if not curr_scene: return ""

    npc_names = curr_scene.get('npcs', [])
    if not npc_names: return ""

    scene_history_key = f"npc_appeared_{curr_id}"
    player_vars = state.get('player_vars', {})
    if player_vars.get(scene_history_key): return ""

    state['player_vars'][scene_history_key] = True
    npc_introductions = []
    action_templates = [
        "당신을 바라봅니다.", "무언가를 하고 있습니다.", "조용히 서 있습니다.",
        "경계하는 눈빛입니다.", "당신을 흥미롭게 쳐다봅니다."
    ]

    for npc_name in npc_names:
        action = random.choice(action_templates)
        intro_html = f"""
        <div class='npc-intro text-green-300 italic my-2 p-2 bg-green-900/20 rounded border-l-2 border-green-500'>
            👀 <span class='font-bold'>{npc_name}</span>이(가) {action}
        </div>
        """
        npc_introductions.append(intro_html)

    return "\n".join(npc_introductions)


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
    # ... (기존 코드 동일) ...
    genre = scenario.get('genre', '').lower()
    world_setting = scenario.get('world_setting', '').lower()

    # 세계관별 폴백 메시지
    fallback_messages = {
        'cyberpunk': "⚠️ 신경 신호가 불안정하여 시야가 일시적으로 차단되었습니다. 잠시 후 다시 시도하십시오.",
        'sf': "⚠️ 통신 간섭이 감지되었습니다. 신호가 안정화될 때까지 대기해 주세요.",
        'fantasy': "⚠️ 마력의 흐름이 일시적으로 혼란스럽습니다. 잠시 정신을 가다듬어 주세요.",
        'horror': "⚠️ 알 수 없는 힘이 시야를 가립니다... 잠시 후 다시 시도해 주세요.",
        'modern': "⚠️ 잠시 정신이 혼미해집니다. 심호흡을 하고 다시 시도해 주세요.",
        'medieval': "⚠️ 갑작스러운 현기증이 엄습합니다. 잠시 쉬었다가 다시 시도해 주세요.",
        'apocalypse': "⚠️ 방사능 간섭으로 인해 감각이 일시적으로 마비되었습니다. 잠시 후 다시 시도하십시오.",
        'workplace': "⚠️ 과로로 인해 잠시 멍해졌습니다. 커피를 마시고 다시 시도해 주세요.",
        'martial': "⚠️ 내공의 흐름이 일시적으로 막혔습니다. 기를 가다듬고 다시 시도하십시오."
    }

    for key, message in fallback_messages.items():
        if key in genre or key in world_setting:
            return message

    return "⚠️ 잠시 상황 파악이 어렵습니다. 심호흡을 하고 다시 시도해 주세요."


def scene_stream_generator(state: PlayerState, retry_count: int = 0, max_retries: int = 2):
    """
    나레이션 스트리밍
    [MODE 1] 힌트 모드 (이동 X)
    [MODE 2] 묘사 모드 (이동 O)
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
    transitions = curr_scene.get('transitions', [])
    trigger_hints = [t.get('trigger', '') for t in transitions if t.get('trigger')]

    # [MODE 1] 씬 유지됨 (탐색/대화) -> 힌트 모드
    if prev_id == curr_id and user_input:

        # [최적화 1] Near Miss 감지 시 LLM 호출 없이 즉시 힌트 반환 (0.01초)
        near_miss = state.get('near_miss_trigger')
        if near_miss:
            yield f"그 행동({user_input})은 되지 않지만, <mark>{near_miss}</mark>와 관련된 무언가가 있을 것 같습니다."
            return

        # [최적화 2] 일반 힌트 생성 시 프롬프트 경량화
        npc_output = state.get('npc_output', '')
        if npc_output:
            yield ""
            return

        # 30% 확률로 LLM 없이 기본 메시지 (비용 절감)
        if random.random() < 0.3:
            yield "특별한 일은 일어나지 않았습니다. 주변을 더 자세히 살펴보세요."
            return

        prompt = f"""
            [Situation] Scene: '{scene_title}'. User tried: "{user_input}" -> Failed.
            [Hidden Triggers] {trigger_hints}
            [Task] Give a VERY short hint (Korean). 1 sentence. Use <mark>tags</mark>.
            """

        try:
            api_key = os.getenv("OPENROUTER_API_KEY")
            llm = LLMFactory.get_llm(api_key=api_key, model_name="openai/tngtech/deepseek-r1t2-chimera:free",
                                     streaming=True)
            for chunk in llm.stream(prompt):
                if chunk.content: yield chunk.content
        except Exception:
            yield "아무런 변화도 없습니다. 다른 것을 찾아보세요."
        return

    # [MODE 2] 씬 변경됨 -> 전체 묘사
    scene_desc = curr_scene.get('description', '')
    npc_names = curr_scene.get('npcs', [])

    npc_intro = check_npc_appearance(state)
    if npc_intro: yield npc_intro + "<br><br>"

    gm_notes = scenario.get('world_settings', '')

    prompt = f"""
    You are a Game Master.
    [SCENE] {scene_desc}
    [GM NOTES] {gm_notes}
    [LOCATION] {scene_title}
    [NPCs] {', '.join(npc_names)}
    [TRIGGERS] {trigger_hints}

    [INSTRUCTIONS]
    1. Rewrite [SCENE] to be immersive (Second-person "You...").
    2. **MANDATORY**: Enclose key interactive objects in <mark> tags.
    3. Korean. 3-5 sentences.
    """

    try:
        api_key = os.getenv("OPENROUTER_API_KEY")
        llm = LLMFactory.get_llm(
            api_key=api_key,
            model_name="openai/tngtech/deepseek-r1t2-chimera:free",
            streaming=True
        )

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