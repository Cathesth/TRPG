from typing import TypedDict, List, Dict, Any, Literal
from langgraph.graph import StateGraph, END
from llm_factory import get_player_model
import os
import json


class PlayerState(TypedDict):
    scenario: Dict
    current_scene_id: str
    player_vars: Dict
    history: List[str]
    last_user_choice_idx: int

    # 내부 처리용
    system_message: str  # 룰 엔진 결과
    npc_output: str  # NPC 대사 (없으면 빈값)
    narrator_output: str  # 나레이션 (검수 전)
    critic_feedback: str  # 검수자 피드백
    retry_count: int  # 재시도 횟수 (무한 루프 방지)


# 1. [Python] 룰 엔진 (계산기 - 환각 Zero)
def rule_node(state: PlayerState):
    scenario = state["scenario"]
    # ... (기존 로직과 동일: 맵핑 및 선택지 유효성 검사) ...
    all_scenes = {s["scene_id"]: s for s in scenario["scenes"]}
    all_endings = {e["ending_id"]: e for e in scenario["endings"]}

    curr_id = state["current_scene_id"]

    # 엔딩 처리
    if curr_id in all_endings:
        return {"system_message": "Game Over", "narrator_output": f"[ENDING] {all_endings[curr_id]['description']}"}

    if curr_id not in all_scenes:
        return {"system_message": "Error: Unknown Scene"}

    current_scene = all_scenes[curr_id]
    choice_idx = state.get("last_user_choice_idx", -1)

    if choice_idx == -1:  # 게임 시작
        return {"system_message": "Game Started.", "retry_count": 0}

    # 선택지 처리 (효과 적용, 이동)
    if not current_scene.get("choices") or choice_idx >= len(current_scene["choices"]):
        return {"system_message": "Invalid Choice"}

    selected_choice = current_scene["choices"][choice_idx]

    # 효과 적용 로직 (기존과 동일)
    new_vars = state["player_vars"].copy()
    logs = []

    if "inventory" not in new_vars: new_vars["inventory"] = []

    if "effects" in selected_choice:
        for effect in selected_choice["effects"]:
            if not isinstance(effect, dict): continue
            target = effect.get("target_var")
            val = effect.get("value")
            op = effect.get("operation")

            if not target: continue

            if target == "inventory":
                if op == "add":
                    new_vars["inventory"].append(val); logs.append(f"획득: {val}")
                elif op == "remove" and val in new_vars["inventory"]:
                    new_vars["inventory"].remove(val)
            else:
                try:
                    int_val = int(val)
                    if op == "add":
                        new_vars[target] = new_vars.get(target, 0) + int_val
                    elif op == "subtract":
                        new_vars[target] = new_vars.get(target, 0) - int_val
                    elif op == "set":
                        new_vars[target] = int_val
                    logs.append(f"{target} {op} {int_val}")
                except:
                    pass

    next_id = selected_choice.get("next_scene_id")
    if not next_id: next_id = curr_id  # 이동 없으면 유지

    return {
        "player_vars": new_vars,
        "current_scene_id": next_id,
        "system_message": f"Result: {', '.join(logs)}",
        "retry_count": 0,  # 턴 바뀔 때 재시도 초기화
        "npc_output": ""  # 초기화
    }


# 2. [LLM] NPC AI Node (전담 마크)
def npc_node(state: PlayerState):
    """
    현재 씬에 NPC가 있다면, 그 중 한 명을 골라 연기시킴.
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key: return {}

    scenario = state["scenario"]
    all_scenes = {s["scene_id"]: s for s in scenario["scenes"]}
    current_scene = all_scenes.get(state["current_scene_id"])

    if not current_scene or not current_scene.get("npc_names"):
        return {"npc_output": ""}  # NPC 없으면 패스

    # 등장하는 첫 번째 NPC 로드 (심화 시 랜덤 or 대화 상대 선택 가능)
    npc_name = current_scene["npc_names"][0]
    npc_data = next((n for n in scenario["npcs"] if n["name"] == npc_name), None)

    if not npc_data: return {"npc_output": ""}

    llm = get_player_model(api_key)

    prompt = f"""
    You are '{npc_data['name']}' in a TRPG game.
    Background: {npc_data['background']}
    Personality: {npc_data['personality']}
    Hostility: {npc_data['hostility']}

    Current Scene: {current_scene['title']}
    Player Status: {state['player_vars']}

    Task: Say something to the player based on the current situation. 
    Keep it short (1-2 sentences). Korean language.
    """

    res = llm.invoke(prompt)
    return {"npc_output": f"{npc_data['name']}: \"{res.content}\""}


# 3. [LLM] Narrator Node (상황 묘사)
def narrator_node(state: PlayerState):
    api_key = os.getenv("OPENROUTER_API_KEY")
    llm = get_player_model(api_key)

    scenario = state["scenario"]

    # 씬 정보 로드
    target_data = None
    for s in scenario["scenes"]:
        if s["scene_id"] == state["current_scene_id"]: target_data = s
    if not target_data:
        for e in scenario["endings"]:
            if e["ending_id"] == state["current_scene_id"]: target_data = e

    prompt = f"""
    Role: TRPG Narrator.
    Scene: {target_data.get('title')}
    Desc: {target_data.get('description')}

    System Log: {state.get('system_message')}
    NPC Action: {state.get('npc_output')}

    Task: Combine the system log, NPC action, and scene description into a cohesive narrative.
    Language: Korean. Max 3 sentences.
    """

    res = llm.invoke(prompt)
    return {"narrator_output": res.content}


# 4. [LLM] Play Critic Node (플레이 검수 AI)
def critic_node(state: PlayerState):
    """
    나레이터와 NPC가 헛소리(환각)를 했는지, 언어는 자연스러운지 감시
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    llm = get_player_model(api_key)

    draft_text = f"{state.get('npc_output', '')}\n{state.get('narrator_output', '')}"

    prompt = f"""
    Role: Game Quality Assurance (Critic).
    Review the following Game Output Text:
    ---
    {draft_text}
    ---
    Global Rules: {state['scenario'].get('global_rules')}
    Player Stats: {state['player_vars']}

    Checklist:
    1. Is the language natural Korean?
    2. Does it contradict the Player Stats? (e.g., saying 'you died' when HP > 0)
    3. Is it consistent with the rules?

    If GOOD, reply exactly: "PASS"
    If BAD, reply with the reason (briefly).
    """

    res = llm.invoke(prompt)
    feedback = res.content.strip()

    if "PASS" in feedback.upper():
        return {"critic_feedback": "PASS"}
    else:
        return {"critic_feedback": feedback, "retry_count": state["retry_count"] + 1}


# --- 라우터 (결정권자) ---
def critic_router(state: PlayerState) -> Literal["pass", "retry"]:
    # 3번 이상 실패하면 그냥 포기하고 통과시킴 (무한루프 방지)
    if state["retry_count"] >= 3:
        return "pass"

    if state["critic_feedback"] == "PASS":
        return "pass"
    else:
        return "retry"


def finalize_node(state: PlayerState):
    """검수 통과된 텍스트를 히스토리에 확정"""
    final_text = f"{state.get('npc_output', '')}\n{state.get('narrator_output', '')}".strip()
    return {"history": [final_text]}


# --- 그래프 조립 ---
def create_game_graph():
    workflow = StateGraph(PlayerState)

    # 노드 등록
    workflow.add_node("rule_engine", rule_node)
    workflow.add_node("npc_actor", npc_node)
    workflow.add_node("narrator", narrator_node)
    workflow.add_node("play_critic", critic_node)
    workflow.add_node("finalize", finalize_node)

    # 흐름 연결
    workflow.set_entry_point("rule_engine")

    # 룰 -> NPC -> 나레이터 -> 비평가
    workflow.add_edge("rule_engine", "npc_actor")
    workflow.add_edge("npc_actor", "narrator")
    workflow.add_edge("narrator", "play_critic")

    # 비평가 -> (조건부) -> 재시도 or 완료
    workflow.add_conditional_edges(
        "play_critic",
        critic_router,
        {
            "pass": "finalize",
            "retry": "narrator"  # 문제 있으면 나레이션 다시 생성
        }
    )

    workflow.add_edge("finalize", END)

    return workflow.compile()