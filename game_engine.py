import random
import json
import logging
import os
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


# Node 1: Intent Parser (수정: Choices -> Transitions)
def intent_parser_node(state: PlayerState):
    user_input = state.get('last_user_input', '').strip()
    logger.info(f"🟢 [USER INPUT]: {user_input}")
    idx = state.get('last_user_choice_idx', -1)

    if idx != -1:
        state['parsed_intent'] = 'choice'
        return state

    scenario = state['scenario']
    curr_scene_id = state['current_scene_id']
    scenes = {s['scene_id']: s for s in scenario['scenes']}
    curr_scene = scenes.get(curr_scene_id)

    # 1. 엔딩 씬인지 확인
    endings = {e['ending_id']: e for e in scenario.get('endings', [])}
    if curr_scene_id in endings:
        state['parsed_intent'] = 'ending'
        return state

    # 2. 트랜지션(이동 조건) 확인
    # 스키마가 변경되어 'choices' 대신 'transitions'를 사용합니다.
    transitions = curr_scene.get('transitions', [])

    # 트랜지션이 없으면 일반 대화로 처리
    if not transitions:
        state['parsed_intent'] = 'chat'
        return state

    # LLM에게 판단 맡기기
    # triggers_text: AI가 판단할 수 있게 트리거 목록 생성
    triggers_text = "\n".join([f"{i + 1}. [ACTION] {t['trigger']}" for i, t in enumerate(transitions)])

    prompt = f"""
    [ROLE]
    You are the 'Intent Classifier' for a TRPG game engine.
    Your job is to determine if the USER INPUT matches any of the HIDDEN TRIGGERS.

    [CURRENT SITUATION]
    Scene: {curr_scene.get('title')}
    Description: {curr_scene.get('description')}

    [HIDDEN TRIGGERS]
    {triggers_text}

    [USER INPUT]
    "{user_input}"

    [TASK]
    1. Analyze if the user's input intends to perform one of the [HIDDEN TRIGGERS].
    2. If the input matches a trigger semantically (even if not exact wording), return "type": "transition" and the "index".
    3. If the input is just talking to an NPC or asking a question, return "type": "chat".
    4. If the input is trying to do something impossible or unrelated, return "type": "chat".

    [OUTPUT FORMAT]
    Return ONLY a JSON object. No markdown.
    Format: {{"type": "transition", "index": <1-based index>}} OR {{"type": "chat"}}
    """

    try:
        api_key = os.getenv("OPENROUTER_API_KEY")
        # 모델: 성능을 위해 builder와 동일한 모델 사용 권장, 여기서는 설정된 모델 사용
        llm = LLMFactory.get_llm(api_key=api_key, model_name="openai/tngtech/deepseek-r1t2-chimera:free")
        response = llm.invoke(prompt).content.strip()

        if "```" in response:
            response = response.split("```")[1].replace("json", "").strip()

        # JSON 파싱 시도
        try:
            result = json.loads(response)
        except:
            # 파싱 실패 시 텍스트에서 type과 index 추출 시도 (Fallback)
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

    logger.info(f"🔍 [INTENT]: {state.get('parsed_intent')} (Choice Index: {state.get('last_user_choice_idx')})")

    return state


# Node 2: Rule Engine (수정: Transitions 처리)
def rule_node(state: PlayerState):
    idx = state['last_user_choice_idx']
    scenario = state['scenario']
    # 현재 씬 ID (변경 전)
    curr_scene_id = state['current_scene_id']

    all_scenes = {s['scene_id']: s for s in scenario['scenes']}
    all_endings = {e['ending_id']: e for e in scenario.get('endings', [])}

    sys_msg = []

    # --- [1] 트랜지션 및 효과 처리 먼저 수행 ---
    curr_scene = all_scenes.get(curr_scene_id)
    transitions = curr_scene.get('transitions', []) if curr_scene else []

    # 선택지가 유효하고 트랜지션 인덱스라면 처리
    if state['parsed_intent'] == 'transition' and transitions and 0 <= idx < len(transitions):
        transition = transitions[idx]
        effects = transition.get('effects', [])
        next_id = transition.get('target_scene_id')
        trigger_desc = transition.get('trigger', '행동')

        # 효과 적용
        for eff in effects:
            try:
                # Effect 객체 구조: {target, type, operation, value}
                if isinstance(eff, dict):
                    key = eff.get("target", "").lower()
                    operation = eff.get("operation", "add")
                    raw_val = eff.get("value", 0)

                    # 값 정수 변환 시도
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

                    # 수치 변수 처리 (hp, gold, sanity 등)
                    if key:
                        current_val = state['player_vars'].get(key, 0)
                        if not isinstance(current_val, int): current_val = 0

                        if operation == "add":
                            new_val = current_val + val
                            sys_msg.append(f"{key.upper()} +{val} (현재: {new_val})")
                        elif operation == "subtract":
                            new_val = max(0, current_val - val)
                            sys_msg.append(f"{key.upper()} -{val} (현재: {new_val})")
                        elif operation == "set":
                            new_val = val
                            sys_msg.append(f"{key.upper()} 설정: {new_val}")
                        else:
                            new_val = current_val

                        state['player_vars'][key] = new_val

            except Exception as e:
                logger.error(f"Effect Error: {e}")
                pass

        # 씬 전환 (ID 업데이트)
        if next_id:
            state['current_scene_id'] = next_id
            sys_msg.append(f"'{trigger_desc}' 행동으로 장면이 전환됩니다.")
            logger.info(f"👣 [SCENE MOVE]: {curr_scene_id} -> {next_id}")

    # --- [2] 변경된 current_scene_id 기준으로 엔딩 체크 ---
    # (트랜지션으로 막 진입했거나, 이미 엔딩 상태이거나 모두 여기서 걸림)
    current_id_after_action = state['current_scene_id']

    if current_id_after_action in all_endings:
        ending = all_endings[current_id_after_action]
        state['parsed_intent'] = 'ending'  # 인텐트 강제 변경 (Narrator 스킵용)
        state['system_message'] = "Game Over"
        state['npc_output'] = ""

        # 엔딩 HTML 생성
        state['narrator_output'] = f"""
        <div class="my-8 p-8 border-2 border-yellow-500/50 bg-gradient-to-b from-yellow-900/40 to-black rounded-xl text-center fade-in shadow-2xl relative overflow-hidden">
            <div class="absolute inset-0 bg-[url('https://www.transparenttextures.com/patterns/stardust.png')] opacity-20"></div>
            <h3 class="text-3xl font-black text-yellow-400 mb-4 tracking-[0.2em] uppercase drop-shadow-md">🎉 ENDING REACHED 🎉</h3>
            <div class="w-16 h-1 bg-yellow-500 mx-auto mb-6 rounded-full"></div>
            <div class="text-2xl font-bold text-white mb-4 drop-shadow-sm">"{ending.get('title')}"</div>
            <p class="text-gray-200 leading-relaxed text-lg font-serif italic">
                {ending.get('description')}
            </p>
            <div class="mt-8 text-xs text-yellow-600/70 font-mono border-t border-yellow-500/20 pt-4">
                THANK YOU FOR PLAYING
            </div>
        </div>
        """
        return state

    # 엔딩이 아니라면 일반 메시지 세팅
    state['npc_output'] = ""
    state['system_message'] = " ".join(sys_msg)

    if sys_msg:
        logger.info(f"⚔️ [RULE EFFECT]: {', '.join(sys_msg)}")

    return state


# Node 3: Narrator (변경 없음, 로직 유지)
def narrator_node(state: PlayerState):
    logger.info("📜 [NARRATOR]: Generating story...")

    # [핵심] 엔딩이거나 이미 엔딩 메시지가 있으면 건너뜀
    if state.get('parsed_intent') == 'ending' or "ENDING REACHED" in state.get('narrator_output', ''):
        return state

    scenario = state['scenario']
    curr_id = state['current_scene_id']
    p_vars = state['player_vars']

    all_scenes = {s['scene_id']: s for s in scenario['scenes']}
    curr_scene = all_scenes.get(curr_id)

    scene_title = curr_scene.get('title') if curr_scene else "Unknown Scene"
    scene_desc = curr_scene.get('description') if curr_scene else "No description available."

    npc_context = f"[NPC SPEAKING]: {state.get('npc_output')}" if state.get('npc_output') else ""

    context = f"""
    [CURRENT SCENE]: {scene_title}
    [DESCRIPTION]: {scene_desc}
    [PLAYER STATUS]: HP={p_vars.get('hp', '?')}, Inventory={p_vars.get('inventory', [])}
    {npc_context}
    [LAST ACTION]: "{state.get('last_user_input')}"
    """

    system_prompt = f"""
    You are the Game Master (Narrator) of a text RPG.
    Describe the result of the player's action and the new situation.
    - If NPC is speaking, include their reaction or dialogue naturally.
    - Keep it immersive, within 3 sentences.
    - Style: {scenario.get('genre', 'Dark Fantasy')}
    - Language: Korean (한국어)
    """

    try:
        api_key = os.getenv("OPENROUTER_API_KEY")
        llm = LLMFactory.get_llm(api_key=api_key, model_name="openai/tngtech/deepseek-r1t2-chimera:free")
        response = llm.invoke(f"{system_prompt}\n\n{context}").content
        state['narrator_output'] = response
    except Exception as e:
        logger.error(f"Narrator Error: {e}")
        state['narrator_output'] = "..."

    logger.info(f"✅ [NARRATOR DONE]: {state.get('narrator_output')[:50]}...")

    return state


# Node 4: NPC Actor (변경 없음)
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

    # 글로벌 NPC 정보 찾기
    npc_info = ""
    for npc in scenario.get('npcs', []):
        if npc.get('name') == target_npc_name:
            npc_info = f"Name: {npc.get('name')}\nPersonality: {npc.get('personality')}\nTone: {npc.get('dialogue_style')}"
            break

    prompt = f"""
    Act as the NPC described below.
    {npc_info}

    Player said: "{user_text}"
    Respond in character. Short (1-2 sentences). Korean.
    """

    try:
        api_key = os.getenv("OPENROUTER_API_KEY")
        llm = LLMFactory.get_llm(api_key=api_key, model_name="openai/tngtech/deepseek-r1t2-chimera:free")
        response = llm.invoke(prompt).content.strip()
        state['npc_output'] = f"{response}"
    except:
        state['npc_output'] = ""

    return state


# Streaming Functions

def narrator_stream_generator(state: PlayerState):
    """스트리밍용 narrator - yield로 토큰을 하나씩 반환"""
    if state.get('parsed_intent') == 'ending' or "ENDING REACHED" in state.get('narrator_output', ''):
        yield state.get('narrator_output', '')
        return

    # Narrator 로직과 동일, but stream() 사용
    # ... (생략 없이 위 narrator_node 로직을 스트리밍으로 구현)

    scenario = state['scenario']
    curr_id = state['current_scene_id']
    p_vars = state['player_vars']

    all_scenes = {s['scene_id']: s for s in scenario['scenes']}
    curr_scene = all_scenes.get(curr_id)

    scene_title = curr_scene.get('title') if curr_scene else "Unknown"
    scene_desc = curr_scene.get('description') if curr_scene else ""

    npc_context = f"[NPC SPEAKING]: {state.get('npc_output')}" if state.get('npc_output') else ""

    context = f"""
    [CURRENT SCENE]: {scene_title}
    [DESCRIPTION]: {scene_desc}
    [PLAYER STATUS]: HP={p_vars.get('hp')}, Inventory={p_vars.get('inventory', [])}
    {npc_context}
    [LAST ACTION]: "{state.get('last_user_input')}"
    """

    system_prompt = f"""
    You are the Game Master (Narrator).
    Describe the result of the player's action and the new situation.
    - If NPC is speaking, incorporate it.
    - Style: {scenario.get('genre', 'General')}
    - Language: Korean (한국어)
    """

    try:
        api_key = os.getenv("OPENROUTER_API_KEY")
        llm = LLMFactory.get_llm(
            api_key=api_key,
            model_name="openai/tngtech/deepseek-r1t2-chimera:free",
            streaming=True
        )

        for chunk in llm.stream(f"{system_prompt}\n\n{context}"):
            if chunk.content:
                yield chunk.content
    except Exception as e:
        logger.error(f"Narrator Streaming Error: {e}")
        yield "..."


# [수정] 프롤로그 스트리밍 생성: "그대로 출력" 요청 반영
def prologue_stream_generator(state: PlayerState):
    """
    프롤로그 텍스트를 그대로 반환 (AI 생성 X)
    """
    scenario = state['scenario']
    # 'prologue' 혹은 'prologue_text' 키 모두 대응
    prologue_text = scenario.get('prologue', scenario.get('prologue_text', ''))

    if not prologue_text:
        yield "이야기가 시작됩니다..."
        return

    # 한 번에 보내거나 조금씩 끊어서 보내는 효과
    # 여기서는 그대로 yield
    yield prologue_text


# 씬 설명 스트리밍 생성
def scene_stream_generator(state: PlayerState):
    """
    현재 씬 설명을 AI가 스트리밍으로 생성
    단, prompt에 "선택지를 나열하지 말 것"을 강조
    """
    scenario = state['scenario']
    curr_id = state['current_scene_id']
    p_vars = state['player_vars']
    genre = scenario.get('genre', 'Dark Fantasy')

    all_scenes = {s['scene_id']: s for s in scenario['scenes']}
    curr_scene = all_scenes.get(curr_id)

    if not curr_scene:
        yield "알 수 없는 장면입니다."
        return

    scene_title = curr_scene.get('title', 'Unknown Scene')
    scene_desc = curr_scene.get('description', '')
    npc_names = curr_scene.get('npcs', [])

    last_action = state.get('last_user_input', '')

    prompt = f"""
    You are a Game Master narrating a TRPG scene transition.

    [GENRE]: {genre}
    [CURRENT SCENE TITLE]: {scene_title}
    [SCENE SETTING]: {scene_desc}
    [NPCs PRESENT]: {', '.join(npc_names) if npc_names else 'None'}
    [PLAYER STATUS]: HP={p_vars.get('hp')}, Inventory={p_vars.get('inventory', [])}
    [LAST ACTION]: "{last_action}"

    Describe this scene vividly as if the player just arrived or just made a choice.
    - Be atmospheric and immersive
    - Describe the environment and any NPCs present
    - Keep it around 3-4 sentences
    - Language: Korean (한국어)
    - IMPORTANT: Do NOT list choices. Just describe the scene.
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
        yield scene_desc if scene_desc else "새로운 장면이 펼쳐집니다..."


# 엔딩 스트리밍 생성 (동일)
def ending_stream_generator(state: PlayerState):
    scenario = state['scenario']
    curr_id = state['current_scene_id']
    p_vars = state['player_vars']
    genre = scenario.get('genre', 'Dark Fantasy')
    title = scenario.get('title', 'Unknown')

    all_endings = {e['ending_id']: e for e in scenario.get('endings', [])}
    ending = all_endings.get(curr_id)

    if not ending:
        yield "엔딩에 도달했습니다."
        return

    ending_title = ending.get('title', 'The End')
    ending_desc = ending.get('description', '')

    prompt = f"""
    You are a Game Master delivering the ending of a TRPG story.

    [GAME TITLE]: {title}
    [GENRE]: {genre}
    [ENDING TITLE]: {ending_title}
    [ENDING DESCRIPTION]: {ending_desc}
    [FINAL PLAYER STATUS]: HP={p_vars.get('hp')}, Inventory={p_vars.get('inventory', [])}

    Write a dramatic, emotional ending narration.
    - Language: Korean (한국어)
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
        logger.error(f"Ending Streaming Error: {e}")
        yield ending_desc if ending_desc else "이야기가 끝났습니다..."


# 전처리 (Streaming용)
def process_before_narrator(state: PlayerState) -> PlayerState:
    # 1. Intent Parser (Choice -> Transition 확인)
    state = intent_parser_node(state)

    intent = state.get('parsed_intent')

    if intent == 'transition' or intent == 'ending':
        # 2. Rule Engine (Transition Effect 적용)
        state = rule_node(state)
    else:
        # 3. NPC Actor (Chat)
        state = npc_node(state)

    return state


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