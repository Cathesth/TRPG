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


def normalize_text(text: str) -> str:
    """텍스트 정규화 (공백 제거, 소문자)"""
    return text.lower().replace(" ", "")


# --- Nodes ---

def intent_parser_node(state: PlayerState):
    """의도 파서 - 예외 처리 강화"""
    user_input = state.get('last_user_input', '').strip()
    norm_input = normalize_text(user_input)
    logger.info(f"🟢 [USER INPUT]: {user_input}")

    # 입력이 없는 경우 처리
    if not user_input:
        state['parsed_intent'] = 'chat'
        state['system_message'] = "행동을 입력해주세요."
        return state

    # 1. 시스템적으로 이미 선택된 경우
    if state.get('last_user_choice_idx', -1) != -1:
        state['parsed_intent'] = 'transition'
        return state

    scenario = state['scenario']
    curr_scene_id = state['current_scene_id']
    scenes = {s['scene_id']: s for s in scenario.get('scenes', [])}

    # 🔥 수정: 씬이 없을 때 처리
    curr_scene = scenes.get(curr_scene_id)
    if not curr_scene:
        logger.warning(f"Current scene not found: {curr_scene_id}")
        state['parsed_intent'] = 'chat'
        state['system_message'] = "현재 위치를 파악할 수 없습니다."
        return state

    # 2. 엔딩 체크
    endings = {e['ending_id']: e for e in scenario.get('endings', [])}
    if curr_scene_id in endings:
        state['parsed_intent'] = 'ending'
        return state

    transitions = curr_scene.get('transitions', [])

    # 트랜지션 없으면 무조건 채팅
    if not transitions:
        state['parsed_intent'] = 'chat'
        return state

    # 3. [Fast-Track] 텍스트 유사도 매칭 (LLM 생략)
    # 파이썬 코드로 직접 비교하므로 속도가 매우 빠름 (0.01초 미만)
    best_idx = -1
    highest_ratio = 0.0

    for idx, trans in enumerate(transitions):
        trigger = trans.get('trigger', '').strip()
        if not trigger: continue
        norm_trigger = normalize_text(trigger)

        # 완전 일치 또는 포함 관계
        if norm_input == norm_trigger or (
                len(norm_input) > 2 and (norm_input in norm_trigger or norm_trigger in norm_input)):
            logger.info(f"⚡ [FAST-TRACK] Direct Match: '{user_input}' matched '{trigger}'")
            state['last_user_choice_idx'] = idx
            state['parsed_intent'] = 'transition'
            return state

        # 유사도 검사 (오타 허용)
        similarity = difflib.SequenceMatcher(None, norm_input, norm_trigger).ratio()
        if similarity > highest_ratio:
            highest_ratio = similarity
            best_idx = idx

    # 유사도가 0.7(70%) 이상이면 AI 호출 없이 바로 인정
    if highest_ratio >= 0.7:
        logger.info(f"⚡ [FAST-TRACK] Fuzzy Match ({highest_ratio:.2f}): '{user_input}' -> Transtion {best_idx}")
        state['last_user_choice_idx'] = best_idx
        state['parsed_intent'] = 'transition'
        return state

    # 4. [Slow-Path] LLM 기반 판단 (최후의 수단)
    triggers_text = "\n".join([f"- {t['trigger']}" for t in transitions])
    prompt = f"""
    [TASK] Match user input to hidden triggers.
    [TRIGGERS]
    {triggers_text}
    [INPUT] "{user_input}"
    [OUTPUT JSON] {{"type": "transition"|"chat", "index": 1-based_index}}
    """
    try:
        api_key = os.getenv("OPENROUTER_API_KEY")
        # 판단용 가벼운 모델 사용
        llm = LLMFactory.get_llm(api_key=api_key, model_name="openai/tngtech/deepseek-r1t2-chimera:free")
        response = llm.invoke(prompt).content.strip()

        # JSON 파싱 시도
        try:
            if "```" in response:
                response = response.split("```")[1].replace("json", "").strip()
            result = json.loads(response)

            if result.get('type') == 'transition':
                idx = int(result.get('index', 0)) - 1
                if 0 <= idx < len(transitions):
                    state['last_user_choice_idx'] = idx
                    state['parsed_intent'] = 'transition'
                    return state
        except:
            pass
    except Exception as e:
        logger.error(f"Intent Parser LLM Error: {e}")

    # 매칭 실패 시 기본값: 채팅
    state['parsed_intent'] = 'chat'
    return state


def rule_node(state: PlayerState):
    """
    Node 2: 규칙 엔진 (이펙트 적용 및 씬 이동)
    - 아이템 획득/분실
    - 스탯(HP, Sanity 등) 변경
    """
    idx = state['last_user_choice_idx']
    scenario = state['scenario']
    curr_scene_id = state['current_scene_id']

    all_scenes = {s['scene_id']: s for s in scenario['scenes']}
    all_endings = {e['ending_id']: e for e in scenario.get('endings', [])}

    sys_msg = []
    curr_scene = all_scenes.get(curr_scene_id)
    transitions = curr_scene.get('transitions', []) if curr_scene else []

    # 트랜지션 실행 조건 충족 시
    if state['parsed_intent'] == 'transition' and 0 <= idx < len(transitions):
        trans = transitions[idx]
        effects = trans.get('effects', [])
        next_id = trans.get('target_scene_id')
        trigger_desc = trans.get('trigger', '행동')

        # --- [이펙트 처리 로직 복구됨] ---
        for eff in effects:
            try:
                if isinstance(eff, dict):
                    key = eff.get("target", "").lower()
                    operation = eff.get("operation", "add")
                    raw_val = eff.get("value", 0)

                    # 값 타입 변환
                    val = 0
                    if isinstance(raw_val, (int, float)):
                        val = int(raw_val)
                    elif isinstance(raw_val, str) and raw_val.isdigit():
                        val = int(raw_val)

                    # A. 아이템 처리 (gain_item / lose_item)
                    if operation in ["gain_item", "lose_item"]:
                        item_name = str(eff.get("value", ""))
                        inventory = state['player_vars'].get('inventory', [])

                        if operation == "gain_item":
                            if item_name not in inventory:
                                inventory.append(item_name)
                                sys_msg.append(f"📦 아이템 획득: {item_name}")
                        elif operation == "lose_item":
                            if item_name in inventory:
                                inventory.remove(item_name)
                                sys_msg.append(f"🗑️ 아이템 사용: {item_name}")

                        state['player_vars']['inventory'] = inventory
                        continue

                    # B. 수치 변수 처리 (HP, Sanity, Gold 등)
                    if key:
                        current_val = state['player_vars'].get(key, 0)
                        if not isinstance(current_val, (int, float)): current_val = 0

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
                logger.error(f"Effect Processing Error: {e}")
                pass

        # 씬 ID 변경
        if next_id:
            state['current_scene_id'] = next_id
            logger.info(f"👣 [MOVE] {curr_scene_id} -> {next_id}")

    # 이동 후 엔딩인지 체크
    if state['current_scene_id'] in all_endings:
        ending = all_endings[state['current_scene_id']]
        state['parsed_intent'] = 'ending'
        # 엔딩 시 나레이터 출력 미리 생성
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

    state['system_message'] = " | ".join(sys_msg)
    return state


def npc_node(state: PlayerState):
    """
    Node 3: NPC 챗봇 (유저가 채팅을 시도했을 때)
    [개선] 대화 내역(History)을 프롬프트에 주입해서 문맥 파악 가능하게 변경
    """
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

    # 첫 번째 NPC가 대답한다고 가정
    target_npc_name = npc_names[0]
    npc_info = f"Name: {target_npc_name}"

    for npc in scenario.get('npcs', []):
        if npc.get('name') == target_npc_name:
            npc_info += f"\nPersonality: {npc.get('personality')}\nTone: {npc.get('dialogue_style')}"
            break

    # [추가됨] 대화 내역 가져오기 (최근 5턴)
    history = state.get('history', [])
    history_context = "\n".join(history[-5:]) if history else "No previous conversation."

    prompt = f"""
    [ROLE] Act as the NPC '{target_npc_name}' in a TRPG.
    [SCENE] Current Location: {curr_scene.get('title')}
    [PROFILE] {npc_info}

    [CONVERSATION HISTORY]
    {history_context}

    [USER SAID] "{user_text}"
    [INSTRUCTION] Respond naturally in character. Keep it short (1-2 sentences). Use Korean.
    """

    try:
        api_key = os.getenv("OPENROUTER_API_KEY")
        llm = LLMFactory.get_llm(api_key=api_key, model_name="openai/tngtech/deepseek-r1t2-chimera:free")
        response = llm.invoke(prompt).content.strip()
        state['npc_output'] = response

        # [추가됨] 대화 내역 저장
        if 'history' not in state: state['history'] = []
        state['history'].append(f"User: {user_text}")
        state['history'].append(f"NPC({target_npc_name}): {response}")

    except Exception as e:
        logger.error(f"NPC LLM Error: {e}")
        state['npc_output'] = "..."

    return state


def check_npc_appearance(state: PlayerState) -> str:
    """
    씬에 등장해야 하는 NPC가 있는지 확인하고 등장 대사를 생성
    """
    scenario = state['scenario']
    curr_id = state['current_scene_id']

    all_scenes = {s['scene_id']: s for s in scenario['scenes']}
    curr_scene = all_scenes.get(curr_id)

    if not curr_scene:
        return ""

    npc_names = curr_scene.get('npcs', [])
    if not npc_names:
        return ""

    # 이미 이 씬에서 NPC를 만났는지 확인
    history = state.get('history', [])
    scene_history_key = f"npc_appeared_{curr_id}"

    # 플레이어 변수에서 이미 등장했는지 확인
    player_vars = state.get('player_vars', {})
    if player_vars.get(scene_history_key):
        return ""

    # NPC 등장 표시
    state['player_vars'][scene_history_key] = True

    # NPC 정보 가져오기
    npc_introductions = []
    for npc_name in npc_names:
        npc_data = None
        for npc in scenario.get('npcs', []):
            if npc.get('name') == npc_name:
                npc_data = npc
                break

        if npc_data:
            # NPC 등장 대사 생성
            try:
                api_key = os.getenv("OPENROUTER_API_KEY")
                llm = LLMFactory.get_llm(api_key=api_key, model_name="openai/tngtech/deepseek-r1t2-chimera:free")

                prompt = f"""
                [TASK] Generate a brief introduction line for an NPC appearing in a scene.
                [NPC NAME] {npc_name}
                [NPC ROLE] {npc_data.get('role', 'Unknown')}
                [NPC PERSONALITY] {npc_data.get('personality', 'Neutral')}
                [SCENE] {curr_scene.get('title', 'Unknown Scene')}
                
                [INSTRUCTION] Write a single Korean sentence (1-2 lines) that the NPC would say when first appearing.
                Keep it natural and in-character. Just the dialogue, no narration.
                """

                response = llm.invoke(prompt).content.strip()
                npc_introductions.append(f"<div class='npc-intro text-green-300 italic my-2'>💬 <span class='font-bold'>{npc_name}</span>: \"{response}\"</div>")
            except Exception as e:
                logger.error(f"NPC Intro Error: {e}")
                npc_introductions.append(f"<div class='npc-intro text-green-300 italic my-2'>💬 <span class='font-bold'>{npc_name}</span>이(가) 나타났다.</div>")
        else:
            npc_introductions.append(f"<div class='npc-intro text-green-300 italic my-2'>💬 <span class='font-bold'>{npc_name}</span>이(가) 나타났다.</div>")

    return "\n".join(npc_introductions)


def narrator_node(state: PlayerState):
    """나레이터 노드 (실제 생성은 스트리밍 함수에서 처리하므로 여기선 패스)"""
    return state


# --- Streaming Generators (SSE) ---

def prologue_stream_generator(state: PlayerState):
    """프롤로그 텍스트 스트리밍"""
    scenario = state['scenario']
    # 프롤로그 텍스트 키가 다를 수 있어서 안전하게 가져옴
    prologue_text = scenario.get('prologue', scenario.get('prologue_text', ''))

    if not prologue_text:
        yield "이야기가 시작됩니다..."
        return

    # 한 번에 보내지 않고 청크 단위로 끊어서 보내거나, 이미 완성된 텍스트면 그냥 보냄
    # 여기서는 단순하게 전체 전송 (LLM 생성이 아니므로)
    yield prologue_text


def scene_stream_generator(state: PlayerState):
    """씬 묘사 스트리밍 - 예외 처리 강화"""
    scenario = state['scenario']
    curr_id = state['current_scene_id']

    all_scenes = {s['scene_id']: s for s in scenario['scenes']}
    all_endings = {e['ending_id']: e for e in scenario.get('endings', [])}

    # 🔥 수정: 엔딩 체크 추가
    if curr_id in all_endings:
        ending = all_endings[curr_id]
        yield f"""
        <div class="ending-scene">
            <h3>🎉 {ending.get('title', 'ENDING')} 🎉</h3>
            <p>{ending.get('description', '이야기가 끝났습니다.')}</p>
        </div>
        """
        return

    curr_scene = all_scenes.get(curr_id)

    # 🔥 수정: 씬이 없을 때 더 나은 fallback
    if not curr_scene:
        logger.warning(f"Scene not found: {curr_id}")
        # 시작 씬으로 리다이렉트 시도
        start_scene_id = scenario.get('start_scene_id')
        if start_scene_id and start_scene_id in all_scenes:
            state['current_scene_id'] = start_scene_id
            yield "잠시 혼란스러웠지만, 정신을 차렸다...<br><br>"
            # 재귀 호출로 시작 씬 출력
            for chunk in scene_stream_generator(state):
                yield chunk
            return
        else:
            yield "어둠 속에서 길을 잃었다. 이야기를 처음부터 시작해야 할 것 같다."
            return

    scene_title = curr_scene.get('title', 'Untitled')
    scene_desc = curr_scene.get('description', '')
    npc_names = curr_scene.get('npcs', [])

    # NPC 등장 확인 및 대사 생성
    npc_intro = check_npc_appearance(state)
    if npc_intro:
        yield npc_intro + "<br><br>"

    transitions = curr_scene.get('transitions', []) if curr_scene else []
    trigger_hints = [t.get('trigger', '') for t in transitions if t.get('trigger')]

    last_action = state.get('last_user_input', '')
    history = state.get('history', [])
    previous_context = "\n".join(history[-3:]) if history else "Game just started."

    # 🔥 수정: builder description을 기반으로 톤만 조정
    prompt = f"""
    You are a Game Master narrating a TRPG scene.

    [BASE DESCRIPTION FROM BUILDER]
    {scene_desc}

    [CONTEXT]
    Title: {scene_title}
    Last Action: "{last_action}"
    NPCs Present: {', '.join(npc_names)}
    Previous Story: {previous_context}

    [HIDDEN TRIGGERS (hint these subtly)]
    {trigger_hints}

    [INSTRUCTIONS]
    1. **Use the BASE DESCRIPTION as your foundation** - keep the core content and atmosphere.
    2. If there was a 'Last Action', describe its immediate result first, then flow into the scene.
    3. Add subtle hints about interactable objects/actions using <mark>tags.
       - Example: "테이블 위에 <mark>녹슨 열쇠</mark>가 놓여있다."
    4. **CRITICAL: NEVER list choices** (no "1. 문 열기" or "What do you want to do?")
    5. Adjust the tone to be immersive and cinematic, but preserve the builder's original content.
    6. Language: Korean
    7. Length: Keep similar to original description length (3-6 sentences)
    """

    try:
        api_key = os.getenv("OPENROUTER_API_KEY")
        llm = LLMFactory.get_llm(
            api_key=api_key,
            model_name="openai/tngtech/deepseek-r1t2-chimera:free",
            streaming=True
        )

        accumulated_text = ""
        for chunk in llm.stream(prompt):
            if chunk.content:
                accumulated_text += chunk.content
                yield chunk.content

        # 🔥 추가: 스트리밍 완료 후 키워드 하이라이트 보정
        # (이미 <mark>가 있으면 건너뛰고, 없으면 추가)
        if "<mark>" not in accumulated_text:
            highlighted = auto_highlight_triggers(accumulated_text, trigger_hints)
            # 차이나는 부분만 추가 전송 (또는 전체 재전송)
            # SSE 특성상 이미 보낸 텍스트는 수정 불가하므로
            # 프롬프트에서 <mark> 사용을 더 강제하는 게 나음
            pass

    except Exception as e:
        logger.error(f"Scene Streaming Error: {e}")
        yield scene_desc if scene_desc else "장면을 불러올 수 없습니다."

def auto_highlight_triggers(text: str, triggers: List[str]) -> str:
    """
    트리거 키워드를 자동으로 <mark> 태그로 감싸기
    (LLM이 놓친 경우 백업용)
    """
    for trigger in triggers:
        # 트리거에서 핵심 키워드 추출 (예: "문을 연다" -> "문")
        keywords = re.findall(r'\b\w{2,}\b', trigger)
        for kw in keywords:
            if kw in text and f"<mark>{kw}</mark>" not in text:
                text = text.replace(kw, f"<mark>{kw}</mark>", 1)  # 첫 등장만
    return text

# --- Graph Construction ---

def create_game_graph():
    """LangGraph 워크플로우 생성"""
    workflow = StateGraph(PlayerState)

    # 노드 등록
    workflow.add_node("intent_parser", intent_parser_node)
    workflow.add_node("rule_engine", rule_node)
    workflow.add_node("npc_actor", npc_node)
    workflow.add_node("narrator", narrator_node)

    # 시작점
    workflow.set_entry_point("intent_parser")

    # 조건부 엣지 (분기 처리)
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

    # 흐름 연결
    workflow.add_edge("rule_engine", "narrator")
    workflow.add_edge("npc_actor", "narrator")
    workflow.add_edge("narrator", END)

    return workflow.compile()