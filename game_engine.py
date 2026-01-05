import random
import json
import logging
import os
import re
import difflib
from typing import TypedDict, List, Dict, Any, Union
from langgraph.graph import StateGraph, END
from llm_factory import LLMFactory
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class PlayerState(TypedDict):
    scenario: Dict[str, Any]
    current_scene_id: str
    player_vars: Dict[str, Any]
    history: List[str]
    last_user_choice_idx: int  # -1이면 선택 안함 / 트랜지션 인덱스로 사용
    last_user_input: str

    parsed_intent: str  # 'transition', 'chat', 'ending', 'unknown'
    system_message: str
    npc_output: str
    narrator_output: str
    critic_feedback: str
    retry_count: int
    chat_log_html: str


def normalize_text(text: str) -> str:
    """공백 제거 및 소문자 변환 (매칭 확률 높이기 위함)"""
    return text.lower().replace(" ", "")


# Node 1: Intent Parser (속도 최적화: Fuzzy Matching 적용)
def intent_parser_node(state: PlayerState):
    user_input = state.get('last_user_input', '').strip()
    norm_input = normalize_text(user_input)
    logger.info(f"🟢 [USER INPUT]: {user_input}")

    # 1. 시스템적으로 이미 선택된 경우 (UI 클릭 등 대비)
    if state.get('last_user_choice_idx', -1) != -1:
        state['parsed_intent'] = 'transition'
        return state

    scenario = state['scenario']
    curr_scene_id = state['current_scene_id']
    scenes = {s['scene_id']: s for s in scenario['scenes']}
    curr_scene = scenes.get(curr_scene_id)

    # 엔딩 체크
    endings = {e['ending_id']: e for e in scenario.get('endings', [])}
    if curr_scene_id in endings:
        state['parsed_intent'] = 'ending'
        return state

    transitions = curr_scene.get('transitions', []) if curr_scene else []

    if not transitions:
        state['parsed_intent'] = 'chat'
        return state

    # --- [Fast-Track] 텍스트 매칭 & 유사도 검사 (LLM 생략) ---

    match_found = False
    best_idx = -1
    highest_ratio = 0.0

    for idx, trans in enumerate(transitions):
        trigger = trans.get('trigger', '').strip()
        if not trigger: continue
        norm_trigger = normalize_text(trigger)

        # 1. 완전 일치 또는 포함 관계
        if norm_input == norm_trigger or (
                len(norm_input) > 2 and (norm_input in norm_trigger or norm_trigger in norm_input)):
            logger.info(f"⚡ [FAST-TRACK] Direct Match: '{user_input}' matched '{trigger}'")
            state['last_user_choice_idx'] = idx
            state['parsed_intent'] = 'transition'
            return state

        # 2. 유사도 검사 (오타 허용) - difflib 사용
        similarity = difflib.SequenceMatcher(None, norm_input, norm_trigger).ratio()
        if similarity > highest_ratio:
            highest_ratio = similarity
            best_idx = idx

    # 유사도가 0.8(80%) 이상이면 AI 호출 없이 바로 인정
    if highest_ratio >= 0.8:
        logger.info(f"⚡ [FAST-TRACK] Fuzzy Match ({highest_ratio:.2f}): '{user_input}' -> Transtion {best_idx}")
        state['last_user_choice_idx'] = best_idx
        state['parsed_intent'] = 'transition'
        return state

    # --- [Slow-Path] LLM 기반 판단 ---

    triggers_text = "\n".join([f"- [HIDDEN ACTION] {t['trigger']}" for t in transitions])

    prompt = f"""
    [ROLE] Intent Classifier for TRPG
    [TASK] Determine if the USER INPUT matches any of the HIDDEN ACTIONS semantically.

    [SCENE CONTEXT]
    Current Scene: {curr_scene.get('title')}

    [HIDDEN ACTIONS (Player doesn't see these)]
    {triggers_text}

    [USER INPUT]
    "{user_input}"

    [INSTRUCTION]
    - The player does NOT know the exact phrasing of hidden actions.
    - If the user's input implies they want to perform one of the hidden actions, match it.
    - If it's just a question, casual chat, or unrelated action, return "chat".

    [OUTPUT JSON ONLY]
    Match found: {{"type": "transition", "index": <1-based index>}}
    No match: {{"type": "chat"}}
    """

    try:
        api_key = os.getenv("OPENROUTER_API_KEY")
        llm = LLMFactory.get_llm(api_key=api_key, model_name="openai/tngtech/deepseek-r1t2-chimera:free")
        response = llm.invoke(prompt).content.strip()

        try:
            if "```" in response:
                response = response.split("```")[1].replace("json", "").strip()
            result = json.loads(response)
        except:
            import re
            type_match = re.search(r'"type":\s*"(\w+)"', response)
            idx_match = re.search(r'"index":\s*(\d+)', response)
            result = {
                "type": type_match.group(1) if type_match else "chat",
                "index": int(idx_match.group(1)) if idx_match else 0
            }

        if result.get('type') == 'transition':
            idx = int(result.get('index', 0)) - 1
            if 0 <= idx < len(transitions):
                state['last_user_choice_idx'] = idx
                state['parsed_intent'] = 'transition'
            else:
                state['parsed_intent'] = 'chat'
        else:
            state['parsed_intent'] = 'chat'

    except Exception as e:
        logger.error(f"[Parser] Error: {e}")
        state['parsed_intent'] = 'chat'

    logger.info(f"🔍 [INTENT]: {state.get('parsed_intent')} (Idx: {state.get('last_user_choice_idx')})")

    return state


# Node 2: Rule Engine
def rule_node(state: PlayerState):
    idx = state['last_user_choice_idx']
    scenario = state['scenario']
    curr_scene_id = state['current_scene_id']

    all_scenes = {s['scene_id']: s for s in scenario['scenes']}
    all_endings = {e['ending_id']: e for e in scenario.get('endings', [])}

    sys_msg = []

    # --- [1] 트랜지션 및 효과 처리 ---
    curr_scene = all_scenes.get(curr_scene_id)
    transitions = curr_scene.get('transitions', []) if curr_scene else []

    if state['parsed_intent'] == 'transition' and transitions and 0 <= idx < len(transitions):
        transition = transitions[idx]
        effects = transition.get('effects', [])
        next_id = transition.get('target_scene_id')
        trigger_desc = transition.get('trigger', '행동')

        # 효과 적용
        for eff in effects:
            try:
                if isinstance(eff, dict):
                    key = eff.get("target", "").lower()
                    operation = eff.get("operation", "add")
                    raw_val = eff.get("value", 0)

                    val = 0
                    if isinstance(raw_val, (int, float)) or (isinstance(raw_val, str) and raw_val.isdigit()):
                        val = int(raw_val)

                    # 아이템 처리
                    if operation in ["gain_item", "lose_item"]:
                        item_name = str(eff.get("value", ""))
                        inventory = state['player_vars'].get('inventory', [])
                        if operation == "gain_item":
                            if item_name not in inventory:
                                inventory.append(item_name)
                                sys_msg.append(f"아이템 획득: {item_name}")
                        elif operation == "lose_item":
                            if item_name in inventory:
                                inventory.remove(item_name)
                                sys_msg.append(f"아이템 소실: {item_name}")
                        state['player_vars']['inventory'] = inventory
                        continue

                    # 수치 변수 처리
                    if key:
                        current_val = state['player_vars'].get(key, 0)
                        if not isinstance(current_val, int): current_val = 0

                        if operation == "add":
                            new_val = current_val + val
                            sys_msg.append(f"{key.upper()} +{val}")
                        elif operation == "subtract":
                            new_val = max(0, current_val - val)
                            sys_msg.append(f"{key.upper()} -{val}")
                        elif operation == "set":
                            new_val = val
                            sys_msg.append(f"{key.upper()} = {new_val}")
                        else:
                            new_val = current_val

                        state['player_vars'][key] = new_val

            except Exception as e:
                logger.error(f"Effect Error: {e}")
                pass

        # 씬 전환 (ID 업데이트)
        if next_id:
            state['current_scene_id'] = next_id
            logger.info(f"👣 [SCENE MOVE]: {curr_scene_id} -> {next_id}")

    # --- [2] 변경된 current_scene_id 기준으로 엔딩 체크 ---
    current_id_after_action = state['current_scene_id']

    if current_id_after_action in all_endings:
        ending = all_endings[current_id_after_action]
        state['parsed_intent'] = 'ending'
        state['system_message'] = "Game Over"
        state['npc_output'] = ""

        # 엔딩 HTML
        state['narrator_output'] = f"""
        <div class="my-8 p-8 border-2 border-yellow-500/50 bg-gradient-to-b from-yellow-900/40 to-black rounded-xl text-center fade-in shadow-2xl relative overflow-hidden">
            <h3 class="text-3xl font-black text-yellow-400 mb-4 tracking-[0.2em] uppercase drop-shadow-md">🎉 ENDING 🎉</h3>
            <div class="w-16 h-1 bg-yellow-500 mx-auto mb-6 rounded-full"></div>
            <div class="text-2xl font-bold text-white mb-4 drop-shadow-sm">"{ending.get('title')}"</div>
            <p class="text-gray-200 leading-relaxed text-lg font-serif italic">
                {ending.get('description')}
            </p>
        </div>
        """
        return state

    state['npc_output'] = ""
    state['system_message'] = " | ".join(sys_msg)
    return state


# Node 3: Narrator (나레이션 생성 - 폴백)
def narrator_node(state: PlayerState):
    return state


# Node 4: NPC Actor (챗봇)
def npc_node(state: PlayerState):
    if state.get('parsed_intent') != 'chat':
        state['npc_output'] = ""
        return state

    scenario = state['scenario']
    user_text = state['last_user_input']
    curr_id = state['current_scene_id']

    all_scenes = {s['scene_id']: s for s in scenario['scenes']}
    curr_scene = all_scenes.get(curr_id)
    npc_names = curr_scene.get('npcs', []) if curr_scene else []

    if not npc_names:
        state['npc_output'] = ""
        return state

    target_npc_name = npc_names[0]
    npc_info = ""
    for npc in scenario.get('npcs', []):
        if npc.get('name') == target_npc_name:
            npc_info = f"Name: {npc.get('name')}\nPersonality: {npc.get('personality')}\nTone: {npc.get('dialogue_style')}"
            break

    prompt = f"""
    [ROLE] Act as the NPC '{target_npc_name}'.
    [PROFILE] {npc_info}
    [USER SAID] "{user_text}"
    [TASK] Respond in character. Short (1-2 sentences). Korean.
    """

    try:
        api_key = os.getenv("OPENROUTER_API_KEY")
        llm = LLMFactory.get_llm(api_key=api_key, model_name="openai/tngtech/deepseek-r1t2-chimera:free")
        response = llm.invoke(prompt).content.strip()
        state['npc_output'] = response
    except:
        state['npc_output'] = ""

    return state


# --- Streaming Functions ---

def narrator_stream_generator(state: PlayerState):
    yield ""


def prologue_stream_generator(state: PlayerState):
    scenario = state['scenario']
    prologue_text = scenario.get('prologue', scenario.get('prologue_text', ''))
    if not prologue_text:
        yield "이야기가 시작됩니다..."
        return
    yield prologue_text


def scene_stream_generator(state: PlayerState):
    """
    씬 묘사 스트리밍 (선택지 미노출 + 힌트 은유적 포함)
    """
    scenario = state['scenario']
    curr_id = state['current_scene_id']
    p_vars = state['player_vars']
    genre = scenario.get('genre', 'Adventure')

    all_scenes = {s['scene_id']: s for s in scenario['scenes']}
    curr_scene = all_scenes.get(curr_id)

    if not curr_scene:
        yield "알 수 없는 장면입니다."
        return

    scene_title = curr_scene.get('title', 'Untitled')
    scene_desc = curr_scene.get('description', '')
    npc_names = curr_scene.get('npcs', [])

    # 힌트용 트리거 정보 (출력용 아님)
    transitions = curr_scene.get('transitions', []) if curr_scene else []
    trigger_hints = [t.get('trigger', '') for t in transitions if t.get('trigger')]

    last_action = state.get('last_user_input', '')

    prompt = f"""
    You are a Game Master narrating a TRPG scene.

    [CONTEXT]
    Title: {scene_title}
    Description: {scene_desc}
    Last Action: "{last_action}"
    NPCs: {', '.join(npc_names)}

    [AVAILABLE HIDDEN ACTIONS]
    {trigger_hints}

    [INSTRUCTIONS]
    1. Describe the scene vividly based on the 'Description' and 'Last Action'.
    2. Naturally weave **subtle hints** about the 'Available Hidden Actions' into the environment description.
       - Use <mark>keyword</mark> to highlight interactable objects.
       - Example: "You see a <mark>rusty key</mark> on the table." (Implying user can take it)
    3. **CRITICAL: DO NOT LIST CHOICES.** - Never write "1. Open door", "2. Run away".
       - Never ask "What do you want to do?".
       - Just describe the situation and let the player type their action.
    4. Language: Korean (한국어).
    5. Length: 3-4 sentences.
    """

    try:
        api_key = os.getenv("OPENROUTER_API_KEY")
        llm = LLMFactory.get_llm(
            api_key=api_key,
            model_name="openai/tngtech/deepseek-r1t2-chimera:free",
            streaming=True
        )

        for chunk in llm.stream(prompt):
            if chunk.content:
                yield chunk.content

    except Exception as e:
        logger.error(f"Scene Streaming Error: {e}")
        yield scene_desc if scene_desc else "..."


# Graph Construction
def create_game_graph():
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

    workflow.add_conditional_edges(
        "intent_parser",
        route_action,
        {
            "rule_engine": "rule_engine",
            "npc_actor": "npc_actor"
        }
    )

    workflow.add_edge("rule_engine", "narrator")
    workflow.add_edge("npc_actor", "narrator")
    workflow.add_edge("narrator", END)

    return workflow.compile()