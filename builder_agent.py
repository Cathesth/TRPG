import os
import json
from typing import TypedDict, List, Annotated, Optional, Dict, Any
import logging
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.runnables import RunnableParallel
from langgraph.graph import StateGraph, END
from pydantic import BaseModel, Field

from llm_factory import LLMFactory
from schemas import NPC

# 로깅 설정
logger = logging.getLogger(__name__)

# --- 전역 콜백 ---
_progress_callback = None


def set_progress_callback(callback):
    global _progress_callback
    _progress_callback = callback


def report_progress(status, step, detail, progress):
    if _progress_callback:
        _progress_callback(status=status, step=step, detail=detail, progress=progress)


# ---------------------------------------------------------
# [중요] 게임 엔진(game_engine.py) 호환 데이터 모델 정의
# ---------------------------------------------------------

class ScenarioSummary(BaseModel):
    title: str = Field(description="시나리오 제목")
    summary: str = Field(description="시나리오 전체 요약")


class World(BaseModel):
    name: str = Field(description="장소 이름")
    description: str = Field(description="상세 묘사")


class Transition(BaseModel):
    """씬 이동 규칙 (Edge -> Transition 변환)"""
    trigger: str = Field(description="유저가 입력할 행동/명령어 (예: 문을 연다, 숲으로 이동한다)")
    target_scene_id: str = Field(description="이동할 씬의 ID")


class GameScene(BaseModel):
    """Playable Scene"""
    scene_id: str = Field(description="씬 ID (노드 ID)")
    title: str = Field(description="씬 제목")
    description: str = Field(description="상황 묘사")
    type: str = Field(description="항상 'scene' 값")
    npcs: List[str] = Field(description="등장 NPC 이름 목록")
    transitions: List[Transition] = Field(description="다음 씬으로 이동하기 위한 행동 목록")


class GameEnding(BaseModel):
    """Ending Scene"""
    ending_id: str = Field(description="엔딩 ID (노드 ID)")
    title: str = Field(description="엔딩 제목")
    description: str = Field(description="엔딩 결과 묘사")
    type: str = Field(description="항상 'ending' 값")


# 리스트 파싱 래퍼
class WorldList(BaseModel):
    worlds: List[World]


class NPCList(BaseModel):
    npcs: List[NPC]


class SceneData(BaseModel):
    """씬과 엔딩을 한 번에 생성하기 위한 래퍼"""
    scenes: List[GameScene]
    endings: List[GameEnding]


# --- 상태 정의 ---
class BuilderState(TypedDict):
    graph_data: Dict[str, Any]
    model_name: str

    blueprint: str
    scenario: dict
    worlds: List[dict]
    characters: List[dict]
    scenes: List[dict]
    endings: List[dict]

    final_data: dict


# ---------------------------------------------------------
# 노드 함수 정의
# ---------------------------------------------------------

def parse_graph_to_blueprint(state: BuilderState):
    """
    그래프(Nodes+Edges)를 LLM이 이해할 수 있는 텍스트 설계도로 변환
    여기서 연결 관계를 명확히 서술해야 LLM이 적절한 Trigger를 생성함.
    """
    report_progress("building", "1/5", "구조 분석 및 설계도 작성...", 10)

    data = state["graph_data"]
    nodes = data.get("nodes", [])
    edges = data.get("edges", [])
    raw_npcs = data.get("npcs", [])

    blueprint = "### 시나리오 구조 명세서 ###\n\n"

    # 1. 설정
    start_node = next((n for n in nodes if n["type"] == "start"), None)
    if start_node:
        blueprint += f"[기본 설정]\n제목: {start_node['data'].get('label', '미정')}\n개요: {start_node['data'].get('description', '')}\n\n"

    # 2. 등장인물
    blueprint += "[등장인물(NPC)]\n"
    for npc in raw_npcs:
        blueprint += f"- {npc.get('name')}: {npc.get('role', '')} ({npc.get('trait', '')})\n"
    blueprint += "\n"

    # 3. 씬 흐름 (가장 중요)
    blueprint += "[장면 및 엔딩 흐름]\n"
    for node in nodes:
        if node["type"] == "start": continue

        node_id = node["id"]
        node_type = node["type"]  # 'scene' or 'ending'
        title = node["data"].get("title", "무제")
        desc = node["data"].get("description", "")

        # 이 노드에서 나가는 연결선 찾기
        outgoing_edges = [e for e in edges if e["source"] == node_id]

        blueprint += f"ID: {node_id} (Type: {node_type})\n"
        blueprint += f"제목: {title}\n"
        blueprint += f"내용 요약: {desc}\n"

        if node_type == 'scene':
            assigned_npcs = node["data"].get("npcs", [])
            blueprint += f"등장 NPC: {', '.join(assigned_npcs) if assigned_npcs else '없음'}\n"

            # 연결 정보 상세 기술 (LLM이 Trigger를 추론할 수 있게)
            if outgoing_edges:
                blueprint += "다음 연결(Transition):\n"
                for edge in outgoing_edges:
                    target_id = edge["target"]
                    target_node = next((n for n in nodes if n["id"] == target_id), None)
                    target_title = target_node["data"].get("title", "알 수 없는 곳") if target_node else target_id
                    target_type = target_node["type"] if target_node else "scene"

                    # LLM에게 힌트: A에서 B로 갈 때 적절한 행동을 만들어라
                    blueprint += f"  -> 목적지 ID: {target_id} ({target_type}), 목적지 제목: {target_title}\n"
            else:
                blueprint += "다음 연결: 없음 (고립된 지역)\n"

        blueprint += "---\n"

    logger.info(f"Generated Blueprint:\n{blueprint}")
    return {"blueprint": blueprint}


def refine_scenario_info(state: BuilderState):
    report_progress("building", "2/5", "시나리오 개요 완성 중...", 30)

    llm = LLMFactory.get_llm(state.get("model_name"))
    parser = JsonOutputParser(pydantic_object=ScenarioSummary)

    prompt = ChatPromptTemplate.from_messages([
        ("system", "당신은 TRPG 시나리오 작가입니다. 설계도를 보고 제목과 요약을 JSON으로 작성하세요.\n{format_instructions}"),
        ("user", "{blueprint}")
    ])

    try:
        result = (prompt | llm | parser).invoke({
            "blueprint": state["blueprint"],
            "format_instructions": parser.get_format_instructions()
        })
        return {"scenario": result}
    except Exception:
        # 실패 시 기본값
        return {"scenario": {"title": "생성된 시나리오", "summary": "내용 없음"}}


def generate_full_content(state: BuilderState):
    """
    NPC, World, Scene, Ending을 병렬로 생성.
    특히 Scene 생성 시 'Transition Trigger'를 창작하는 것이 핵심.
    """
    report_progress("building", "3/5", "장면 및 상호작용 생성 중...", 60)

    llm = LLMFactory.get_llm(state.get("model_name"))

    # 1. NPC 생성
    npc_chain = (
            ChatPromptTemplate.from_messages([
                ("system", "설계도에 있는 NPC들의 상세 설정(성격, 말투 등)을 생성하세요.\n{format_instructions}"),
                ("user", "{blueprint}")
            ])
            | llm
            | JsonOutputParser(pydantic_object=NPCList)
    )

    # 2. 세계관 생성
    world_chain = (
            ChatPromptTemplate.from_messages([
                ("system", "시나리오 배경 장소 3~4곳을 분위기 있게 묘사하세요.\n{format_instructions}"),
                ("user", "{blueprint}")
            ])
            | llm
            | JsonOutputParser(pydantic_object=WorldList)
    )

    # 3. 씬 & 엔딩 통합 생성 (가장 중요)
    # 그래프 구조를 유지하면서 내용을 채워야 함
    scene_prompt = ChatPromptTemplate.from_messages([
        ("system",
         "당신은 게임 레벨 디자이너입니다. 설계도(Blueprint)를 바탕으로 실제 플레이 가능한 씬 데이터를 JSON으로 생성하세요.\n"
         "중요 규칙:\n"
         "1. 'ID'는 설계도에 명시된 값을 **절대 변경하지 말고 그대로** 사용하세요.\n"
         "2. 각 씬의 'transitions'를 생성할 때, 설계도의 연결 정보를 보고 자연스러운 행동(trigger)을 만드세요.\n"
         "   (예: 숲 입구 -> 숲 내부 연결이면 trigger='숲으로 들어간다')\n"
         "3. 엔딩(ending) 노드도 빠짐없이 생성하세요.\n"
         "{format_instructions}"),
        ("user", "{blueprint}")
    ])

    # 병렬 실행
    chain = RunnableParallel(
        npcs=npc_chain.partial(format_instructions=JsonOutputParser(pydantic_object=NPCList).get_format_instructions()),
        worlds=world_chain.partial(
            format_instructions=JsonOutputParser(pydantic_object=WorldList).get_format_instructions()),
        content=(scene_prompt | llm | JsonOutputParser(pydantic_object=SceneData))
    )

    try:
        results = chain.invoke({"blueprint": state["blueprint"], "format_instructions": JsonOutputParser(
            pydantic_object=SceneData).get_format_instructions()})

        # 결과 파싱
        npcs = results['npcs'].get('npcs', []) if isinstance(results['npcs'], dict) else results['npcs']
        worlds = results['worlds'].get('worlds', []) if isinstance(results['worlds'], dict) else results['worlds']

        content = results['content']
        scenes = content.get('scenes', [])
        endings = content.get('endings', [])

        return {
            "characters": npcs,
            "worlds": worlds,
            "scenes": scenes,
            "endings": endings
        }

    except Exception as e:
        logger.error(f"Generation Error: {e}")
        return {"characters": [], "worlds": [], "scenes": [], "endings": []}


def finalize_build(state: BuilderState):
    report_progress("building", "5/5", "데이터 검증 및 완료...", 95)

    # 시작 씬 찾기 logic
    nodes = state["graph_data"].get("nodes", [])
    edges = state["graph_data"].get("edges", [])

    start_node = next((n for n in nodes if n["type"] == "start"), None)
    start_scene_id = None

    # 1. Start 노드와 직접 연결된 Scene 찾기
    if start_node:
        start_edge = next((e for e in edges if e["source"] == start_node["id"]), None)
        if start_edge:
            start_scene_id = start_edge["target"]

    # 2. 없으면 첫 번째 Scene 노드를 시작점으로
    if not start_scene_id:
        first_scene = next((n for n in nodes if n["type"] == "scene"), None)
        if first_scene:
            start_scene_id = first_scene["id"]

    # 최종 데이터 조립 (game_engine.py 호환 구조)
    final_data = {
        "title": state["scenario"].get("title", "Untitled"),
        "desc": state["scenario"].get("summary", ""),  # api.py list_scenarios 호환
        "scenario": state["scenario"],
        "worlds": state["worlds"],
        "npcs": state["characters"],
        "scenes": state["scenes"],  # [{scene_id, transitions: [{trigger, target_scene_id}]}]
        "endings": state["endings"],  # [{ending_id, title...}]
        "start_scene_id": start_scene_id,
        "raw_graph": state["graph_data"]
    }

    return {"final_data": final_data}


# ---------------------------------------------------------
# 실행 함수
# ---------------------------------------------------------

def build_builder_graph():
    workflow = StateGraph(BuilderState)
    workflow.add_node("parse", parse_graph_to_blueprint)
    workflow.add_node("refine", refine_scenario_info)
    workflow.add_node("generate", generate_full_content)
    workflow.add_node("finalize", finalize_build)

    workflow.set_entry_point("parse")
    workflow.add_edge("parse", "refine")
    workflow.add_edge("refine", "generate")
    workflow.add_edge("generate", "finalize")
    workflow.add_edge("finalize", END)

    return workflow.compile()


def generate_scenario_from_graph(api_key, user_data, model_name=None):
    app = build_builder_graph()
    if not model_name and 'model' in user_data:
        model_name = user_data['model']

    initial_state = {
        "graph_data": user_data,
        "model_name": model_name,
        "blueprint": "",
        "scenario": {},
        "worlds": [],
        "characters": [],
        "scenes": [],
        "endings": [],
        "final_data": {}
    }
    result = app.invoke(initial_state)
    return result['final_data']


def generate_single_npc(scenario_title: str, scenario_summary: str, user_request: str = "", model_name: str = None):
    # 기존 코드 유지 (팝업용)
    llm = LLMFactory.get_llm(model_name)
    parser = JsonOutputParser(pydantic_object=NPC)
    prompt = ChatPromptTemplate.from_messages([
        ("system", "TRPG 캐릭터 생성기입니다.\n{format_instructions}"),
        ("user", f"제목: {scenario_title}\n설정: {scenario_summary}\n요청: {user_request}")
    ])
    chain = prompt | llm | parser
    try:
        return chain.invoke({"format_instructions": parser.get_format_instructions()})
    except Exception as e:
        return {"error": str(e)}