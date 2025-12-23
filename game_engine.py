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
    last_user_choice_idx: int
    last_user_input: str

    parsed_intent: str
    system_message: str
    npc_output: str
    narrator_output: str
    critic_feedback: str
    retry_count: int
    chat_log_html: str


# Node 1: Intent Parser
def intent_parser_node(state: PlayerState):
    user_input = state.get('last_user_input', '').strip()
    idx = state.get('last_user_choice_idx', -1)

    if idx != -1:
        state['parsed_intent'] = 'choice'
        return state

    scenario = state['scenario']
    curr_scene_id = state['current_scene_id']
    scenes = {s['scene_id']: s for s in scenario['scenes']}
    curr_scene = scenes.get(curr_scene_id)

    # 엔딩 씬인지 확인
    endings = {e['ending_id']: e for e in scenario.get('endings', [])}
    if curr_scene_id in endings:
        state['parsed_intent'] = 'ending'
        return state

    if not curr_scene or not curr_scene.get('choices'):
        state['parsed_intent'] = 'unknown'
        return state

    choices_text = "\n".join([f"{i + 1}. {c['text']}" for i, c in enumerate(curr_scene['choices'])])

    prompt = f"""
    [ROLE]
    You are a fast intent classifier for a text RPG.
    Analyze the USER INPUT and match it to one of the CHOICES.

    [CHOICES]
    {choices_text}

    [USER INPUT]
    "{user_input}"

    [OUTPUT FORMAT]
    Return ONLY a JSON object. No markdown.
    Format: {{"type": "choice", "index": <number 1-based>}} OR {{"type": "chat"}}
    """

    try:
        api_key = os.getenv("OPENROUTER_API_KEY")
        # [수정] 모델: TNG DeepSeek R1T2 Chimera
        llm = LLMFactory.get_llm(api_key=api_key, model_name="openai/tngtech/deepseek-r1t2-chimera:free")
        response = llm.invoke(prompt).content.strip()

        if "```" in response:
            response = response.split("```")[1].replace("json", "").strip()

        result = json.loads(response)

        if result.get('type') == 'choice':
            state['last_user_choice_idx'] = int(result.get('index', 0)) - 1
            state['parsed_intent'] = 'choice'
        else:
            state['parsed_intent'] = 'chat'

    except Exception as e:
        logger.error(f"[Parser] Error: {e}")
        state['parsed_intent'] = 'chat'

    return state


# Node 2: Rule Engine
def rule_node(state: PlayerState):
    idx = state['last_user_choice_idx']
    scenario = state['scenario']
    curr_scene_id = state['current_scene_id']

    all_scenes = {s['scene_id']: s for s in scenario['scenes']}
    all_endings = {e['ending_id']: e for e in scenario.get('endings', [])}

    # [핵심 수정] 엔딩 도달 시 처리
    if curr_scene_id in all_endings:
        ending = all_endings[curr_scene_id]
        state['parsed_intent'] = 'ending'  # 엔딩 상태 확정
        state['system_message'] = "Game Over"
        state['npc_output'] = ""
        # 엔딩 메시지 (HTML 스타일 적용)
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

    curr_scene = all_scenes.get(curr_scene_id)
    sys_msg = []

    if curr_scene and 'choices' in curr_scene and 0 <= idx < len(curr_scene['choices']):
        choice = curr_scene['choices'][idx]
        effects = choice.get('effects', [])
        next_id = choice.get('next_scene_id')

        for eff in effects:
            key = None
            val = 0
            try:
                if isinstance(eff, dict):
                    key = eff.get("type", "").lower()
                    raw_val = eff.get("value", 0)
                    try:
                        val = int(raw_val)
                    except:
                        val = 0
                elif isinstance(eff, str):
                    parts = eff.split()
                    if len(parts) >= 2:
                        key = parts[0].lower()
                        try:
                            val = int(parts[1])
                        except:
                            val = 0

                if key:
                    old_val = state['player_vars'].get(key, 0)
                    if key == 'hp':
                        state['player_vars']['hp'] = max(0, old_val + val)
                        sign = "+" if val > 0 else ""
                        sys_msg.append(f"체력 {sign}{val} (현재: {state['player_vars']['hp']})")
                    elif key == 'gold':
                        state['player_vars']['gold'] = max(0, old_val + val)
                        sign = "+" if val > 0 else ""
                        sys_msg.append(f"골드 {sign}{val} (현재: {state['player_vars']['gold']})")
                    elif key == 'item_get':
                        inventory = state['player_vars'].get('inventory', [])
                        item_name = str(eff.get('value', '')) if isinstance(eff, dict) else parts[1]
                        if item_name:
                            inventory.append(item_name)
                            state['player_vars']['inventory'] = inventory
                            sys_msg.append(f"아이템 획득: {item_name}")
            except:
                pass

        if next_id:
            state['current_scene_id'] = next_id
            sys_msg.append(f"장면이 전환됩니다.")

    state['npc_output'] = ""
    state['system_message'] = " ".join(sys_msg)
    return state


# Node 3: Narrator
def narrator_node(state: PlayerState):
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
    [PLAYER STATUS]: HP={p_vars.get('hp')}, Inventory={p_vars.get('inventory')}
    {npc_context}
    [LAST ACTION]: User chose choice #{state['last_user_choice_idx'] + 1} or said "{state.get('last_user_input')}"
    """

    system_prompt = f"""
    You are the Game Master (Narrator) of a text RPG.
    Describe the result of the player's action and the new situation.
    - If NPC is speaking, include their reaction or dialogue naturally.
    - Keep it immersive, within 3 sentences.
    - Style: {scenario.get('theme', 'Dark Fantasy')}
    - Language: Korean (한국어)
    """

    try:
        api_key = os.getenv("OPENROUTER_API_KEY")
        # [수정] 모델: TNG DeepSeek R1T2 Chimera
        llm = LLMFactory.get_llm(api_key=api_key, model_name="openai/tngtech/deepseek-r1t2-chimera:free")
        response = llm.invoke(f"{system_prompt}\n\n{context}").content
        state['narrator_output'] = response
    except Exception as e:
        logger.error(f"Narrator Error: {e}")
        state['narrator_output'] = "..."

    return state


# Node 4: NPC Actor
def npc_node(state: PlayerState):
    if state.get('parsed_intent') != 'chat':
        state['npc_output'] = ""
        return state

    scenario = state['scenario']
    user_text = state['last_user_input']

    curr_id = state['current_scene_id']
    all_scenes = {s['scene_id']: s for s in scenario['scenes']}
    curr_scene = all_scenes.get(curr_id)

    npc_names = curr_scene.get('npc_names', []) if curr_scene else []

    if not npc_names:
        state['npc_output'] = ""
        return state

    target_npc = npc_names[0]

    prompt = f"""
    Act as the NPC '{target_npc}'.
    Player said: "{user_text}"
    Respond in character. Short (1 sentence). Korean.
    """

    try:
        api_key = os.getenv("OPENROUTER_API_KEY")
        # [수정] 모델: TNG DeepSeek R1T2 Chimera
        llm = LLMFactory.get_llm(api_key=api_key, model_name="openai/tngtech/deepseek-r1t2-chimera:free")
        response = llm.invoke(prompt).content.strip()
        state['npc_output'] = f"{response}"
    except:
        state['npc_output'] = ""

    return state


# Streaming Narrator Generator (스트리밍 전용)
def narrator_stream_generator(state: PlayerState):
    """
    스트리밍용 narrator - yield로 토큰을 하나씩 반환
    """
    # 엔딩이거나 이미 엔딩 메시지가 있으면 건너뜀
    if state.get('parsed_intent') == 'ending' or "ENDING REACHED" in state.get('narrator_output', ''):
        yield state.get('narrator_output', '')
        return

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
    [PLAYER STATUS]: HP={p_vars.get('hp')}, Inventory={p_vars.get('inventory')}
    {npc_context}
    [LAST ACTION]: User chose choice #{state['last_user_choice_idx'] + 1} or said "{state.get('last_user_input')}"
    """

    system_prompt = f"""
    You are the Game Master (Narrator) of a text RPG.
    Describe the result of the player's action and the new situation.
    - If NPC is speaking, include their reaction or dialogue naturally.
    - Keep it immersive, within 3 sentences.
    - Style: {scenario.get('theme', 'Dark Fantasy')}
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


# 프롤로그 스트리밍 생성
def prologue_stream_generator(state: PlayerState):
    """
    프롤로그를 AI가 스트리밍으로 생성
    """
    scenario = state['scenario']
    prologue_text = scenario.get('prologue_text', '')
    theme = scenario.get('theme', 'Dark Fantasy')
    title = scenario.get('title', 'Unknown')

    prompt = f"""
    You are a Game Master starting a new TRPG session.
    Based on the following prologue setting, create an immersive opening narration.
    
    [GAME TITLE]: {title}
    [THEME]: {theme}
    [PROLOGUE SETTING]: {prologue_text}
    
    Write a dramatic, atmospheric opening that draws the player into the world.
    - Be descriptive and set the mood
    - Keep it around 3-5 sentences
    - Language: Korean (한국어)
    - Do NOT include any choices or options
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
        logger.error(f"Prologue Streaming Error: {e}")
        yield prologue_text if prologue_text else "게임이 시작됩니다..."


# 씬 설명 스트리밍 생성
def scene_stream_generator(state: PlayerState):
    """
    현재 씬 설명을 AI가 스트리밍으로 생성
    """
    scenario = state['scenario']
    curr_id = state['current_scene_id']
    p_vars = state['player_vars']
    theme = scenario.get('theme', 'Dark Fantasy')

    all_scenes = {s['scene_id']: s for s in scenario['scenes']}
    curr_scene = all_scenes.get(curr_id)

    if not curr_scene:
        yield "알 수 없는 장면입니다."
        return

    scene_title = curr_scene.get('title', 'Unknown Scene')
    scene_desc = curr_scene.get('description', '')
    npc_names = curr_scene.get('npc_names', [])

    last_action = state.get('last_user_input', '')
    choice_idx = state.get('last_user_choice_idx', -1)

    # 이전 씬에서의 선택 정보
    action_context = ""
    if choice_idx >= 0 and last_action:
        action_context = f"[PLAYER'S LAST ACTION]: The player chose option {choice_idx + 1} or said '{last_action}'"

    prompt = f"""
    You are a Game Master narrating a TRPG scene transition.
    
    [THEME]: {theme}
    [CURRENT SCENE TITLE]: {scene_title}
    [SCENE SETTING]: {scene_desc}
    [NPCs PRESENT]: {', '.join(npc_names) if npc_names else 'None'}
    [PLAYER STATUS]: HP={p_vars.get('hp')}, Inventory={p_vars.get('inventory', [])}
    {action_context}
    
    Describe this scene vividly as if the player just arrived or just made a choice.
    - Be atmospheric and immersive
    - Describe the environment and any NPCs present
    - Keep it around 3-4 sentences
    - Language: Korean (한국어)
    - Do NOT list choices, just describe the scene
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


# 엔딩 스트리밍 생성
def ending_stream_generator(state: PlayerState):
    """
    엔딩을 AI가 스트리밍으로 생성
    """
    scenario = state['scenario']
    curr_id = state['current_scene_id']
    p_vars = state['player_vars']
    theme = scenario.get('theme', 'Dark Fantasy')
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
    [THEME]: {theme}
    [ENDING TITLE]: {ending_title}
    [ENDING DESCRIPTION]: {ending_desc}
    [FINAL PLAYER STATUS]: HP={p_vars.get('hp')}, Inventory={p_vars.get('inventory', [])}
    
    Write a dramatic, emotional ending narration that concludes the player's journey.
    - Be poetic and conclusive
    - Reflect on the player's choices and journey
    - Make it memorable and impactful
    - Keep it around 4-6 sentences
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


# 스트리밍 없이 전처리만 수행하는 함수 (intent_parser + rule_engine + npc_actor)
def process_before_narrator(state: PlayerState) -> PlayerState:
    """
    narrator 전까지의 처리를 수행하고 state 반환
    스트리밍 모드에서 사용
    """
    # Intent Parser
    state = intent_parser_node(state)

    intent = state.get('parsed_intent')

    if intent == 'choice' or intent == 'ending':
        # Rule Engine
        state = rule_node(state)
    else:
        # NPC Actor
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
        if intent == 'choice' or intent == 'ending':  # 엔딩도 룰 엔진으로 보냄
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