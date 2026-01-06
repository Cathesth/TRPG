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
from core.utils import renumber_scenes_bfs

logger = logging.getLogger(__name__)

# --- 전역 콜백 ---
_progress_callback = None


def set_progress_callback(callback):
    global _progress_callback
    _progress_callback = callback


def report_progress(status, step, detail, progress):
    if _progress_callback:
        _progress_callback(status=status, step=step, detail=detail, progress=progress)


# --- [유틸리티] JSON 파싱 헬퍼 (백업에서 복원) ---
def parse_json_garbage(text: str) -> dict:
    """
    LLM 응답에서 JSON을 안전하게 추출
    마크다운 코드블록, 이중 인코딩 등을 처리
    """
    if isinstance(text, dict):
        return text
    if not text:
        return {}
    try:
        text = text.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]

        parsed = json.loads(text)
        if isinstance(parsed, str):
            try:
                parsed = json.loads(parsed)
            except:
                pass
        return parsed if isinstance(parsed, dict) else {}
    except:
        try:
            start = text.find('{')
            end = text.rfind('}') + 1
            if start != -1 and end > start:
                return json.loads(text[start:end])
        except:
            pass
        return {}


# --- 데이터 모델 ---

class ScenarioSummary(BaseModel):
    title: str
    summary: str


class World(BaseModel):
    name: str
    description: str


class Transition(BaseModel):
    trigger: str = Field(description="행동 (예: 문을 연다)")
    target_scene_id: str


class GameScene(BaseModel):
    scene_id: str
    name: str
    description: str
    type: str
    npcs: List[str]
    transitions: List[Transition]


class GameEnding(BaseModel):
    ending_id: str
    title: str
    description: str
    type: str


class WorldList(BaseModel):
    worlds: List[World]


class NPCList(BaseModel):
    npcs: List[NPC]


class SceneData(BaseModel):
    scenes: List[GameScene]
    endings: List[GameEnding]


# [New Models for Fast Patch]
class EndingPatch(BaseModel):
    ending_id: str = Field(description="수정할 엔딩의 ID")
    description: str = Field(description="새로 작성된 엔딩 설명")


class TransitionPatch(BaseModel):
    scene_id: str = Field(description="트랜지션이 있는 씬 ID")
    target_scene_id: str = Field(description="목적지 씬 ID")
    new_trigger: str = Field(description="수정된 행동(Trigger)")


class PatchResult(BaseModel):
    endings: List[EndingPatch] = Field(description="수정된 엔딩 목록")
    transitions: List[TransitionPatch] = Field(description="수정된 트랜지션 목록")


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


# --- 노드 함수 ---

def parse_graph_to_blueprint(state: BuilderState):
    report_progress("building", "1/5", "구조 분석 중...", 10)
    data = state["graph_data"]
    nodes = data.get("nodes", [])
    edges = data.get("edges", [])
    raw_npcs = data.get("npcs", [])

    blueprint = "### 시나리오 구조 명세서 ###\n\n"

    start_node = next((n for n in nodes if n["type"] == "start"), None)
    if start_node:
        blueprint += f"[설정]\n제목: {start_node['data'].get('label', '')}\n개요: {start_node['data'].get('description', '')}\n\n"

    blueprint += "[등장인물]\n"
    for npc in raw_npcs:
        blueprint += f"- {npc.get('name')}: {npc.get('role', '')}\n"
    blueprint += "\n"

    blueprint += "[장면 흐름]\n"
    for node in nodes:
        if node["type"] == "start": continue
        node_id = node["id"]
        title = node["data"].get("title", "제목 없음")
        desc = node["data"].get("description", "")
        outgoing = [e for e in edges if e["source"] == node_id]

        blueprint += f"ID: {node_id} ({node['type']})\n제목: {title}\n설명: {desc}\n"
        if outgoing:
            blueprint += "연결:\n"
            for e in outgoing:
                blueprint += f"  -> 목적지: {e['target']}\n"
        blueprint += "---\n"

    return {"blueprint": blueprint}


def refine_scenario_info(state: BuilderState):
    report_progress("building", "2/5", "개요 작성 중...", 30)
    llm = LLMFactory.get_llm(state.get("model_name"))
    parser = JsonOutputParser(pydantic_object=ScenarioSummary)
    prompt = ChatPromptTemplate.from_messages([
        ("system", "TRPG 시나리오 작가입니다. JSON으로 작성하세요.\n{format_instructions}"),
        ("user", "{blueprint}")
    ])
    try:
        res = (prompt | llm | parser).invoke({
            "blueprint": state["blueprint"],
            "format_instructions": parser.get_format_instructions()
        })
        return {"scenario": res}
    except:
        return {"scenario": {"title": "Untitled", "summary": ""}}


def generate_full_content(state: BuilderState):
    report_progress("building", "3/5", "장면 생성 중...", 60)
    llm = LLMFactory.get_llm(state.get("model_name"))

    # 블루프린트에서 시나리오 정보 추출
    blueprint = state.get("blueprint", "")

    # NPC
    npc_parser = JsonOutputParser(pydantic_object=NPCList)
    npc_chain = (
            ChatPromptTemplate.from_messages([
                ("system", "NPC 상세 설정을 생성하세요. 설계도에 없는 NPC는 추가하지 마세요.\n{format_instructions}"),
                ("user", "{blueprint}")
            ]).partial(format_instructions=npc_parser.get_format_instructions())
            | llm | npc_parser
    )

    # World - 프롤로그 세계관 반영 강화
    world_parser = JsonOutputParser(pydantic_object=WorldList)
    world_chain = (
            ChatPromptTemplate.from_messages([
                ("system",
                 "설계도에 명시된 세계관/배경 설정을 정확히 반영하여 배경 장소 3곳을 묘사하세요.\n"
                 "설계도의 '개요'에 적힌 시대, 장르, 분위기를 반드시 따르세요.\n"
                 "예: 개요가 '사이버펑크'라면 미래 도시를, '1930년대'라면 그 시대 배경을 만드세요.\n"
                 "{format_instructions}"),
                ("user", "{blueprint}")
            ]).partial(format_instructions=world_parser.get_format_instructions())
            | llm | world_parser
    )

    # Scene (초안) - 설계도 반영 강화
    scene_parser = JsonOutputParser(pydantic_object=SceneData)
    scene_prompt = ChatPromptTemplate.from_messages([
        ("system",
         "설계도를 바탕으로 씬 데이터를 생성하세요.\n"
         "중요: 설계도에 명시된 세계관, 시대적 배경, 분위기를 정확히 반영하세요.\n"
         "ID는 절대 변경하지 마세요.\n"
         "연결(Transition) 생성 시 '이동한다' 같은 표현 대신 '문을 연다', '살펴본다' 등 구체적인 행동을 만드세요.\n"
         "각 씬의 description은 이전 씬과 자연스럽게 연결되어야 합니다.\n"
         "엔딩 설명이 비어있으면 창작해서 채우세요.\n"
         "{format_instructions}"),
        ("user", "{blueprint}")
    ]).partial(format_instructions=scene_parser.get_format_instructions())
    scene_chain = scene_prompt | llm | scene_parser

    chain = RunnableParallel(npcs=npc_chain, worlds=world_chain, content=scene_chain)

    try:
        res = chain.invoke({"blueprint": state["blueprint"]})
        content = res['content']
        return {
            "characters": res['npcs'].get('npcs', []),
            "worlds": res['worlds'].get('worlds', []),
            "scenes": content.get('scenes', []),
            "endings": content.get('endings', [])
        }
    except Exception as e:
        logger.error(f"Gen Error: {e}")
        return {"characters": [], "worlds": [], "scenes": [], "endings": []}


def polish_content(state: BuilderState):
    """
    [Validator Node - Fast Patch 방식]
    전체를 재생성하지 않고, 문제가 발견된 항목만 부분적으로 수정(Patch)하여 반영함.
    """
    report_progress("building", "4/5", "품질 검수 및 부분 수정 중...", 80)
    llm = LLMFactory.get_llm(state.get("model_name"))

    scenes = state["scenes"]
    endings = state["endings"]
    scenario_title = state["scenario"].get("title", "")

    # 1. 수정 대상 식별
    items_to_fix = []

    # 엔딩 검사 (설명이 비었거나 너무 짧으면 보강)
    for end in endings:
        if not end.get("description") or len(end.get("description")) < 10:
            items_to_fix.append(f"[엔딩 보강] ID '{end.get('ending_id')}' ({end.get('title')}): 설명이 비어있거나 너무 짧음.")

    # 트리거 검사 (너무 단순한 트리거 수정)
    for scene in scenes:
        for trans in scene.get("transitions", []):
            trig = trans.get("trigger", "")
            # "이동한다", "Move" 같은 단순한 트리거는 수정 대상
            if "이동" in trig or "Move" in trig or len(trig) < 2:
                items_to_fix.append(
                    f"[트리거 수정] Scene '{scene.get('scene_id')}' -> '{trans.get('target_scene_id')}': 현재 '{trig}'는 너무 밋밋함. 구체적 행동으로 변경.")

    # 수정할 게 없으면 바로 리턴 (시간 절약)
    if not items_to_fix:
        return state

    # 2. LLM에게 부분 수정 요청 (Patch)
    prompt = ChatPromptTemplate.from_messages([
        ("system",
         "당신은 TRPG 시나리오 에디터입니다. 지적된 문제점들을 해결하여 '수정된 데이터만' JSON으로 출력하세요.\n"
         "전체 데이터를 다시 쓰지 말고, 변경이 필요한 항목만 리스트에 담아주세요.\n"
         "{format_instructions}"),
        ("user",
         f"시나리오 제목: {scenario_title}\n\n"
         f"수정 요청 사항:\n" + "\n".join(items_to_fix) + "\n\n"
                                                    "위 항목들에 대해 창의적인 내용을 채워서 응답하세요.")
    ])

    parser = JsonOutputParser(pydantic_object=PatchResult)
    chain = prompt | llm | parser

    try:
        # format_instructions을 invoke 시점에 전달
        patch_data = chain.invoke({"format_instructions": parser.get_format_instructions()})

        # 3. 원본 데이터에 패치 적용 (In-place Update)

        # 엔딩 업데이트
        updates_endings = {p['ending_id']: p['description'] for p in patch_data.get('endings', [])}
        if updates_endings:
            for end in endings:
                if end['ending_id'] in updates_endings:
                    end['description'] = updates_endings[end['ending_id']]
                    logger.info(f"Patched Ending: {end['ending_id']}")

        # 트랜지션 업데이트
        updates_transitions = {(p['scene_id'], p['target_scene_id']): p['new_trigger'] for p in
                               patch_data.get('transitions', [])}
        if updates_transitions:
            for scene in scenes:
                for trans in scene.get('transitions', []):
                    key = (scene['scene_id'], trans['target_scene_id'])
                    if key in updates_transitions:
                        trans['trigger'] = updates_transitions[key]
                        logger.info(f"Patched Transition: {key} -> {trans['trigger']}")

        # 상태 업데이트
        return {
            "scenes": scenes,
            "endings": endings
        }

    except Exception as e:
        logger.error(f"Polish Patch Error: {e}")
        return state  # 에러 나면 그냥 원본 유지


def finalize_build(state: BuilderState):
    report_progress("building", "5/5", "완료!", 100)
    data = state["graph_data"]
    start_id = None

    # 시작점 찾기
    start_node = next((n for n in data.get("nodes", []) if n["type"] == "start"), None)
    if start_node:
        edge = next((e for e in data.get("edges", []) if e["source"] == start_node["id"]), None)
        if edge: start_id = edge["target"]

    if not start_id and state["scenes"]:
        start_id = state["scenes"][0]["scene_id"]

    # 프롤로그 연결 설정
    prologue_connects = []
    if start_node:
        for edge in data.get("edges", []):
            if edge["source"] == start_node["id"]:
                prologue_connects.append(edge["target"])

    final_data = {
        "title": state["scenario"].get("title", "Untitled"),
        "desc": state["scenario"].get("summary", ""),
        "prologue": start_node.get("data", {}).get("description", "") if start_node else "",
        "prologue_text": start_node.get("data", {}).get("description", "") if start_node else "",
        "prologue_connects_to": prologue_connects,
        "scenario": state["scenario"],
        "worlds": state["worlds"],
        "npcs": state["characters"],
        "scenes": state["scenes"],
        "events": state["scenes"],  # 호환성
        "endings": state["endings"],
        "start_scene_id": start_id,
        "raw_graph": state["graph_data"]
    }

    # 씬 번호 재정렬 (BFS 순서로)
    final_data = renumber_scenes_bfs(final_data)

    return {"final_data": final_data}


def build_builder_graph():
    workflow = StateGraph(BuilderState)
    workflow.add_node("parse", parse_graph_to_blueprint)
    workflow.add_node("refine", refine_scenario_info)
    workflow.add_node("generate", generate_full_content)
    workflow.add_node("polish", polish_content)  # [추가된 Validator 노드 (Patch 방식)]
    workflow.add_node("finalize", finalize_build)

    workflow.set_entry_point("parse")
    workflow.add_edge("parse", "refine")
    workflow.add_edge("refine", "generate")
    workflow.add_edge("generate", "polish")  # generate -> polish
    workflow.add_edge("polish", "finalize")  # polish -> finalize
    workflow.add_edge("finalize", END)
    return workflow.compile()


# ... (generate_scenario_from_graph 등 하단 함수는 동일하므로 생략하지 않고 그대로 유지해야 함) ...
def generate_scenario_from_graph(api_key, user_data, model_name=None):
    app = build_builder_graph()
    if not model_name and 'model' in user_data:
        model_name = user_data['model']
    initial_state = {
        "graph_data": user_data,
        "model_name": model_name,
        "blueprint": "", "scenario": {}, "worlds": [], "characters": [], "scenes": [], "endings": [], "final_data": {}
    }
    return app.invoke(initial_state)['final_data']


def generate_single_npc(scenario_title, scenario_summary, user_request="", model_name=None):
    llm = LLMFactory.get_llm(model_name)
    parser = JsonOutputParser(pydantic_object=NPC)
    prompt = ChatPromptTemplate.from_messages([
        ("system", "캐릭터 생성기\n{format_instructions}"),
        ("user", f"제목:{scenario_title}\n요청:{user_request}")
    ]).partial(format_instructions=parser.get_format_instructions())
    return (prompt | llm | parser).invoke({})

