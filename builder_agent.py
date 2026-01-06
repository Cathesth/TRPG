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

# --- 전역 콜백 (진행 상황 공유용) ---
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
    title: str = Field(description="씬 제목")
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
    graph_data: Dict[str, Any]  # nodes, edges, npcs 원본 데이터
    model_name: str

    # 처리된 데이터
    blueprint: str  # 그래프를 텍스트로 변환한 명세서
    scenario: dict  # 타이틀/요약
    worlds: List[dict]  # 세계관
    characters: List[dict]  # NPC 리스트 (상세)
    scenes: List[dict]  # 씬 리스트 (그래프 구조 반영)

    final_data: dict  # 최종 결과


# ---------------------------------------------------------
# 노드 함수 정의
# ---------------------------------------------------------

def parse_graph_to_blueprint(state: BuilderState):
    """
    프론트엔드에서 받은 그래프 데이터(nodes, edges)를
    LLM이 이해할 수 있는 텍스트 명세서(Blueprint)로 변환
    """
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
        title = node["data"].get("title", "제목 없음")
        desc = node["data"].get("description", "")
        node_npcs = node["data"].get("npcs", [])

        # 연결된 다음 씬 찾기
        connected_ids = [e["target"] for e in edges if e["source"] == node_id]

        blueprint += f"ID: {node_id} ({node['type']})\n"
        blueprint += f"제목: {title}\n"
        blueprint += f"내용: {desc}\n"
        blueprint += f"등장 NPC: {', '.join(node_npcs) if node_npcs else '없음'}\n"
        blueprint += f"다음 연결: {connected_ids}\n"
        blueprint += "---\n"

    logger.info(f"Generated Blueprint:\n{blueprint}")
    return {"blueprint": blueprint}


def refine_scenario_info(state: BuilderState):
    """Start 노드 정보를 바탕으로 시나리오 제목과 요약을 다듬음"""
    report_progress("building", "2/5", "시나리오 개요 다듬는 중...", 30)

    llm = LLMFactory.get_llm(state.get("model_name"))
    parser = JsonOutputParser(pydantic_object=ScenarioSummary)

    prompt = ChatPromptTemplate.from_messages([
        ("system",
         "당신은 TRPG 시나리오 에디터입니다. 주어진 '시나리오 설정'을 바탕으로 매력적인 제목과 요약을 작성하세요.\n"
         "- 입력된 내용이 부실하면 살을 붙여서 완성하세요.\n"
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
    except Exception as e:
        logger.error(f"Scenario refine error: {e}")
        # 실패 시 그래프 데이터 그대로 사용
        nodes = state["graph_data"].get("nodes", [])
        start_node = next((n for n in nodes if n["type"] == "start"), None)
        return {"scenario": {
            "title": start_node['data'].get('label', '제목 없음') if start_node else "제목 없음",
            "summary": start_node['data'].get('description', '') if start_node else ""
        }}


def generate_details_and_scenes(state: BuilderState):
    """세계관, NPC 상세, 그리고 씬 내용을 동시에 생성"""
    report_progress("building", "3/5", "상세 설정 및 장면 생성 중...", 60)

    llm = LLMFactory.get_llm(state.get("model_name"))

    # 1. 세계관 & NPC 생성 체인
    # 이미 사용자가 입력한 NPC 정보(raw_npcs)가 있지만, LLM을 통해 스탯 등을 채워넣음
    npc_parser = JsonOutputParser(pydantic_object=NPCList)
    npc_prompt = ChatPromptTemplate.from_messages([
        ("system",
         "주어진 '등장인물 목록'과 '시나리오 설정'을 참고하여, 등장하는 모든 NPC의 상세 설정(스탯 포함)을 생성하세요.\n"
         "- 목록에 없는 NPC를 임의로 추가하지 마세요.\n"
         "- 각 캐릭터의 특징을 시나리오 분위기에 맞게 구체화하세요.\n"
         "{format_instructions}"),
        ("user", "{blueprint}")
    ])

    world_parser = JsonOutputParser(pydantic_object=WorldList)
    world_prompt = ChatPromptTemplate.from_messages([
        ("system",
         "시나리오의 배경이 되는 장소(World) 3~4곳을 설정하세요.\n"
         "- 시나리오의 분위기에 맞는 장소들을 묘사하세요.\n"
         "{format_instructions}"),
        ("user", "{blueprint}")
    ])

    # 2. 씬 상세 생성 체인 (가장 중요)
    # 그래프의 노드 구조를 유지하면서 내용을 풍성하게 만듦
    scene_parser = JsonOutputParser(pydantic_object=SceneList)
    scene_prompt = ChatPromptTemplate.from_messages([
        ("system",
         "주어진 '장면 흐름' 명세서를 바탕으로 실제 게임에서 사용될 씬 데이터를 생성하세요.\n"
         "- ID는 명세서에 있는 것을 **반드시 그대로** 유지해야 합니다.\n"
         "- '내용'을 플레이어에게 보여줄 생동감 넘치는 묘사로 확장하세요.\n"
         "- 'next_scenes' 연결 관계도 명세서 그대로 유지하세요.\n"
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
            "format_instructions": "JSON format only."  # 간단하게 처리 (각 parser가 알아서 함)
        })

        # 결과 처리
        npcs = results['npcs'].get('npcs', []) if isinstance(results['npcs'], dict) else results['npcs']
        worlds = results['worlds'].get('worlds', []) if isinstance(results['worlds'], dict) else results['worlds']
        scenes = results['scenes'].get('scenes', []) if isinstance(results['scenes'], dict) else results['scenes']

        return {"characters": npcs, "worlds": worlds, "scenes": scenes}

    except Exception as e:
        logger.error(f"Parallel gen error: {e}")
        return {"characters": [], "worlds": [], "scenes": []}


def finalize_build(state: BuilderState):
    """최종 데이터 조립"""
    report_progress("building", "5/5", "최종 데이터 병합 중...", 95)

    # 씬 리스트를 딕셔너리나 더 사용하기 편한 구조로 변환할 수도 있음
    # 여기서는 리스트 그대로 유지하되, Start 노드 정보도 Scene에 포함되어 있는지 확인

    final_data = {
        "title": state["scenario"].get("title", "Untitled"),
        "scenario_info": state["scenario"],  # {title, summary}
        "worlds": state["worlds"],
        "npcs": state["characters"],  # 플레이어 뷰에서 'npcs' 키를 기대할 수 있음
        "scenes": state["scenes"],  # 'events' 대신 'scenes' 사용 (그래프 구조 반영)

        # 그래프 원본 데이터도 백업용으로 포함
        "raw_graph": state["graph_data"]
    }

    return {"final_data": final_data}


# ---------------------------------------------------------
# 그래프 빌드
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


# ---------------------------------------------------------
# 외부 호출 함수
# ---------------------------------------------------------

def generate_scenario_from_graph(api_key, user_data, model_name=None):
    """
    api.py의 init_game에서 호출됨.
    user_data는 {nodes: [], edges: [], npcs: [], model: ...} 형태임.
    """
    app = build_builder_graph()

    # model_name이 user_data에 있을 수도 있음 (builder_view에서 보냄)
    if not model_name and 'model' in user_data:
        model_name = user_data['model']

    initial_state = {
        "graph_data": user_data,  # 전체 데이터를 graph_data로 넘김
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
    """단일 NPC 생성 함수 (팝업용) - 기존 유지"""
    llm = LLMFactory.get_llm(model_name)
    parser = JsonOutputParser(pydantic_object=NPC)

    prompt_text = (
        f"시나리오: {scenario_title}\n{scenario_summary}\n\n"
        f"요청: {user_request if user_request else '어울리는 NPC 1명'}\n\n"
        "위 설정에 맞는 NPC 1명을 JSON으로 생성하세요."
    )

    prompt = ChatPromptTemplate.from_messages([
        ("system", "당신은 TRPG 캐릭터 디자이너입니다. 다음 JSON 스키마를 정확히 준수하여 응답하세요.\n{format_instructions}"),
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