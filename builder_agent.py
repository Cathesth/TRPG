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
# 데이터 모델 정의
# ---------------------------------------------------------

class ScenarioSummary(BaseModel):
    """시나리오 제목 및 개요"""
    title: str = Field(description="시나리오 제목")
    summary: str = Field(description="시나리오 전체 줄거리 요약")


class World(BaseModel):
    """세계관/장소 정보"""
    name: str = Field(description="장소 이름")
    description: str = Field(description="상세 묘사")


class GameScene(BaseModel):
    """실제 게임에서 사용될 씬 정보"""
    id: str = Field(description="씬의 고유 ID (노드 ID와 동일하게)")
    name: str = Field(description="씬 제목 (기존 호환성을 위해 title 대신 name 사용)")
    description: str = Field(description="씬의 상황 묘사 및 전개 내용")
    type: str = Field(description="씬 타입 (start, scene, ending 등)")
    npcs: List[str] = Field(description="이 씬에 등장하는 NPC 이름 목록")
    next_scenes: List[str] = Field(description="이 씬에서 연결되는 다음 씬들의 ID 목록")


class WorldList(BaseModel):
    worlds: List[World]


class NPCList(BaseModel):
    npcs: List[NPC]


class SceneList(BaseModel):
    scenes: List[GameScene]


# --- 상태 정의 (State) ---
class BuilderState(TypedDict):
    graph_data: Dict[str, Any]
    model_name: str

    blueprint: str
    scenario: dict
    worlds: List[dict]
    characters: List[dict]
    scenes: List[dict]

    final_data: dict


# ---------------------------------------------------------
# 노드 함수 정의
# ---------------------------------------------------------

def parse_graph_to_blueprint(state: BuilderState):
    """그래프 데이터를 텍스트 명세서로 변환"""
    report_progress("building", "1/5", "시나리오 구조 분석 중...", 10)

    data = state["graph_data"]
    nodes = data.get("nodes", [])
    edges = data.get("edges", [])
    raw_npcs = data.get("npcs", [])

    blueprint = "### 시나리오 구조 명세서 ###\n\n"

    # 1. 설정 (Start Node)
    start_node = next((n for n in nodes if n["type"] == "start"), None)
    if start_node:
        blueprint += f"[시나리오 설정]\n제목: {start_node['data'].get('label', '미정')}\n개요: {start_node['data'].get('description', '')}\n\n"

    # 2. 등장인물
    blueprint += "[등장인물 목록]\n"
    for npc in raw_npcs:
        blueprint += f"- {npc.get('name')}: {npc.get('role', '역할 미정')} ({npc.get('trait', '')})\n"
    blueprint += "\n"

    # 3. 씬 흐름
    blueprint += "[장면 흐름]\n"
    for node in nodes:
        if node["type"] == "start": continue

        node_id = node["id"]
        # 기존 title을 name으로 매핑하기 위한 준비
        title = node["data"].get("title", "제목 없음")
        desc = node["data"].get("description", "")
        node_npcs = node["data"].get("npcs", [])

        # 연결된 다음 씬 찾기
        connected_ids = [e["target"] for e in edges if e["source"] == node_id]

        blueprint += f"ID: {node_id} ({node['type']})\n"
        blueprint += f"제목(Name): {title}\n"
        blueprint += f"내용: {desc}\n"
        blueprint += f"등장 NPC: {', '.join(node_npcs) if node_npcs else '없음'}\n"
        blueprint += f"다음 연결: {connected_ids}\n"
        blueprint += "---\n"

    logger.info(f"Generated Blueprint:\n{blueprint}")
    return {"blueprint": blueprint}


def refine_scenario_info(state: BuilderState):
    report_progress("building", "2/5", "시나리오 개요 다듬는 중...", 30)

    llm = LLMFactory.get_llm(state.get("model_name"))
    parser = JsonOutputParser(pydantic_object=ScenarioSummary)

    prompt = ChatPromptTemplate.from_messages([
        ("system",
         "당신은 TRPG 시나리오 에디터입니다. 주어진 '시나리오 설정'을 바탕으로 매력적인 제목과 요약을 작성하세요.\n"
         "- JSON 형식으로 출력하세요.\n"
         "{format_instructions}"),
        ("user", "{blueprint}")
    ])

    try:
        result = (prompt | llm | parser).invoke({
            "blueprint": state["blueprint"],
            "format_instructions": parser.get_format_instructions()
        })
        return {"scenario": result}
    except Exception:
        nodes = state["graph_data"].get("nodes", [])
        start_node = next((n for n in nodes if n["type"] == "start"), None)
        return {"scenario": {
            "title": start_node['data'].get('label', '제목 없음') if start_node else "제목 없음",
            "summary": start_node['data'].get('description', '') if start_node else ""
        }}


def generate_details_and_scenes(state: BuilderState):
    report_progress("building", "3/5", "상세 설정 및 장면 생성 중...", 60)

    llm = LLMFactory.get_llm(state.get("model_name"))

    # NPC 상세 생성
    npc_parser = JsonOutputParser(pydantic_object=NPCList)
    npc_prompt = ChatPromptTemplate.from_messages([
        ("system", "주어진 목록의 NPC 상세 설정을 생성하세요.\n{format_instructions}"),
        ("user", "{blueprint}")
    ])

    # 세계관 생성
    world_parser = JsonOutputParser(pydantic_object=WorldList)
    world_prompt = ChatPromptTemplate.from_messages([
        ("system", "시나리오 배경이 되는 장소 3~4곳을 묘사하세요.\n{format_instructions}"),
        ("user", "{blueprint}")
    ])

    # 씬 상세 생성 (GameScene 구조 사용: name 필드 중요)
    scene_parser = JsonOutputParser(pydantic_object=SceneList)
    scene_prompt = ChatPromptTemplate.from_messages([
        ("system",
         "주어진 '장면 흐름' 명세서를 바탕으로 씬 데이터를 생성하세요.\n"
         "- ID와 next_scenes는 명세서 그대로 유지하세요.\n"
         "- '제목'은 'name' 필드에 저장하세요.\n"
         "{format_instructions}"),
        ("user", "{blueprint}")
    ])

    parallel_chain = RunnableParallel(
        npcs=npc_prompt | llm | npc_parser,
        worlds=world_prompt | llm | world_parser,
        scenes=scene_prompt | llm | scene_parser
    )

    try:
        results = parallel_chain.invoke({
            "blueprint": state["blueprint"],
            "format_instructions": "JSON format only."
        })

        npcs = results['npcs'].get('npcs', []) if isinstance(results['npcs'], dict) else results['npcs']
        worlds = results['worlds'].get('worlds', []) if isinstance(results['worlds'], dict) else results['worlds']
        scenes = results['scenes'].get('scenes', []) if isinstance(results['scenes'], dict) else results['scenes']

        return {"characters": npcs, "worlds": worlds, "scenes": scenes}

    except Exception as e:
        logger.error(f"Parallel gen error: {e}")
        return {"characters": [], "worlds": [], "scenes": []}


def finalize_build(state: BuilderState):
    report_progress("building", "5/5", "최종 데이터 병합 중...", 95)

    # 1. 시작 씬 ID 계산 (start_scene_id)
    # Start 노드와 연결된 첫 번째 씬을 찾아야 게임이 시작됨
    nodes = state["graph_data"].get("nodes", [])
    edges = state["graph_data"].get("edges", [])

    start_node = next((n for n in nodes if n["type"] == "start"), None)
    start_scene_id = None

    if start_node:
        # Start 노드에서 나가는 엣지 찾기
        start_edge = next((e for e in edges if e["source"] == start_node["id"]), None)
        if start_edge:
            start_scene_id = start_edge["target"]

    # 연결된 씬 없으면 노드 리스트 중 첫 번째 Scene 타입 노드 사용 (비상용)
    if not start_scene_id:
        first_scene_node = next((n for n in nodes if n["type"] == "scene"), None)
        if first_scene_node:
            start_scene_id = first_scene_node["id"]

    final_data = {
        "title": state["scenario"].get("title", "Untitled"),
        "scenario_info": state["scenario"],
        "worlds": state["worlds"],
        "npcs": state["characters"],

        # [중요] 호환성 필드
        "scenes": state["scenes"],  # 최신 코드용
        "events": state["scenes"],  # 레거시 코드 호환용 (이름만 다르고 내용은 같음)
        "start_scene_id": start_scene_id,  # [필수] 게임 엔진 초기화용

        "raw_graph": state["graph_data"]
    }

    return {"final_data": final_data}


# ---------------------------------------------------------
# 그래프 빌드 및 실행 함수 (변동 없음)
# ---------------------------------------------------------

def build_builder_graph():
    workflow = StateGraph(BuilderState)
    workflow.add_node("parse_graph", parse_graph_to_blueprint)
    workflow.add_node("refine_info", refine_scenario_info)
    workflow.add_node("generate_content", generate_details_and_scenes)
    workflow.add_node("finalize", finalize_build)

    workflow.set_entry_point("parse_graph")
    workflow.add_edge("parse_graph", "refine_info")
    workflow.add_edge("refine_info", "generate_content")
    workflow.add_edge("generate_content", "finalize")
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
        "final_data": {}
    }
    result = app.invoke(initial_state)
    return result['final_data']


def generate_single_npc(scenario_title: str, scenario_summary: str, user_request: str = "", model_name: str = None):
    llm = LLMFactory.get_llm(model_name)
    parser = JsonOutputParser(pydantic_object=NPC)
    prompt_text = (
        f"시나리오: {scenario_title}\n{scenario_summary}\n\n"
        f"요청: {user_request if user_request else '어울리는 NPC 1명'}\n\n"
        "위 설정에 맞는 NPC 1명을 JSON으로 생성하세요."
    )
    prompt = ChatPromptTemplate.from_messages([
        ("system", "당신은 TRPG 캐릭터 디자이너입니다. JSON 스키마를 준수하세요.\n{format_instructions}"),
        ("user", "{prompt_text}")
    ])
    chain = prompt | llm | parser
    try:
        return chain.invoke({
            "prompt_text": prompt_text,
            "format_instructions": parser.get_format_instructions()
        })
    except Exception as e:
        logger.error(f"NPC Gen Error: {e}")
        return {"error": str(e)}