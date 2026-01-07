import json
import os
from typing import TypedDict, List, Annotated, Optional, Dict, Any
import logging
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.runnables import RunnableParallel
from langgraph.graph import StateGraph, END

from llm_factory import LLMFactory
from schemas import NPC
from core.utils import renumber_scenes_bfs

from pydantic import BaseModel, Field
from typing import Optional, List

logger = logging.getLogger(__name__)

# --- 전역 콜백 ---
_progress_callback = None


def set_progress_callback(callback):
    global _progress_callback
    _progress_callback = callback


def report_progress(status, step, detail, progress, phase=None):
    if _progress_callback:
        payload = {
            "status": status,
            "step": step,
            "detail": detail,
            "progress": progress,
            "current_phase": phase or "initializing"
        }
        _progress_callback(**payload)


# --- [유틸리티] JSON 파싱 헬퍼 ---
def parse_json_garbage(text: str) -> dict:
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
    title: str = Field(description="시나리오 제목")
    summary: str = Field(description="시나리오 전체 줄거리 요약")
    player_prologue: str = Field(description="[공개용 프롤로그] 게임 시작 시 화면에 출력되어 플레이어가 읽게 될 도입부 텍스트.")
    gm_notes: str = Field(description="[시스템 내부 설정] 플레이어에게는 비밀로 하고 시스템(GM)이 관리할 전체 설정, 진실, 트릭 등.")


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
    type: str = Field(description="장면 유형 (normal 또는 battle)")
    background: Optional[str] = Field(None, description="배경 묘사")
    trigger: Optional[str] = Field(None, description="이 장면으로 진입하거나 다음으로 넘어가기 위한 핵심 트리거/조건")
    npcs: List[str]
    enemies: Optional[List[str]] = Field(None, description="등장하는 적 목록")
    rule: Optional[str] = Field(None, description="추가 룰")
    transitions: List[Transition]


class GameEnding(BaseModel):
    ending_id: str
    title: str
    description: str
    background: Optional[str] = Field(None, description="엔딩 배경 묘사")
    type: str


class WorldList(BaseModel):
    worlds: List[World]


class NPCList(BaseModel):
    npcs: List[NPC]


class SceneData(BaseModel):
    scenes: List[GameScene]
    endings: List[GameEnding]


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

def validate_structure(state: BuilderState):
    """
    구조 검증: 노드 연결 상태 및 필수 데이터 확인
    """
    logger.info("Validating graph structure...")
    report_progress("building", "0/5", "구조 및 연결 검증 중...", 5, phase="initializing")

    graph_data = state["graph_data"]
    nodes = graph_data.get("nodes", [])
    edges = graph_data.get("edges", [])

    node_map = {n["id"]: n for n in nodes}
    edge_map = {n["id"]: {"in": [], "out": []} for n in nodes}

    for edge in edges:
        src = edge["source"]
        tgt = edge["target"]
        if src in edge_map:
            edge_map[src]["out"].append(tgt)
        if tgt in edge_map:
            edge_map[tgt]["in"].append(src)

    for node in nodes:
        nid = node["id"]
        ntype = node["type"]
        data = node["data"]
        title = data.get("label") if ntype == "start" else data.get("title", "제목 없음")

        # 1. Start Node Validation
        if ntype == "start":
            if not data.get("label") or not data.get("prologue") or not data.get("gm_notes") or not data.get(
                    "background"):
                # 빌더 UI에서 막지만, API 레벨에서도 한번 더 검증
                pass  # 일단 경고만 하거나 패스 (사용자 경험상 UI 차단이 우선)

            has_valid_next = False
            for target_id in edge_map[nid]["out"]:
                target_node = node_map.get(target_id)
                if target_node and target_node["type"] == "scene":
                    has_valid_next = True
                    break
            if not has_valid_next:
                raise ValueError("프롤로그에 다음 장면을 연결해주세요.")

        # 2. Scene Node Validation
        elif ntype == "scene":
            # 필수 항목: 제목, 배경, 트리거
            # if not data.get("title") or not data.get("background") or not data.get("trigger"):
            #    raise ValueError(f"'{title}' 장면의 필수 항목(제목, 배경, 트리거)이 누락되었습니다.")

            # Input Check
            has_valid_prev = False
            for prev_id in edge_map[nid]["in"]:
                prev_node = node_map.get(prev_id)
                if prev_node and prev_node["type"] in ["start", "scene"]:
                    has_valid_prev = True
                    break
            if not has_valid_prev:
                raise ValueError(f"'{title}' 장면의 앞의 장면을 올바르게 연결해주세요.")

            # Output Check
            has_valid_next = False
            for target_id in edge_map[nid]["out"]:
                target_node = node_map.get(target_id)
                if target_node and target_node["type"] in ["scene", "ending"]:
                    has_valid_next = True
                    break
            if not has_valid_next:
                raise ValueError(f"'{title}' 장면의 뒤의 장면을 올바르게 연결해주세요.")

        # 3. Ending Node Validation
        elif ntype == "ending":
            # 필수 항목: 배경, 엔딩 문구(description)
            # if not data.get("background") or not data.get("description"):
            #     raise ValueError(f"'{title}' 엔딩의 필수 항목(배경, 엔딩 문구)이 누락되었습니다.")

            has_valid_prev = False
            for prev_id in edge_map[nid]["in"]:
                prev_node = node_map.get(prev_id)
                if prev_node and prev_node["type"] == "scene":
                    has_valid_prev = True
                    break
            if not has_valid_prev:
                raise ValueError(f"'{title}' 엔딩 앞의 장면을 연결해주세요.")

    return state


def parse_graph_to_blueprint(state: BuilderState):
    report_progress("building", "1/5", "구조 분석 중...", 10, phase="parsing")
    data = state["graph_data"]
    nodes = data.get("nodes", [])
    edges = data.get("edges", [])
    raw_npcs = data.get("npcs", [])

    blueprint = "### 시나리오 구조 명세서 ###\n\n"

    start_node = next((n for n in nodes if n["type"] == "start"), None)
    if start_node:
        title = start_node['data'].get('label', '')
        prologue = start_node['data'].get('prologue', '')
        gm_notes = start_node['data'].get('gm_notes', '')
        bg = start_node['data'].get('background', '')

        blueprint += f"[설정]\n제목: {title}\n"
        if prologue: blueprint += f"프롤로그: {prologue}\n"
        if gm_notes: blueprint += f"시스템 설정: {gm_notes}\n"
        if bg: blueprint += f"배경 묘사: {bg}\n"
        blueprint += "\n"

    blueprint += "[등장인물]\n"
    for npc in raw_npcs:
        blueprint += f"- {npc.get('name')}: {npc.get('role', '')}\n"
    blueprint += "\n"

    blueprint += "[장면 흐름]\n"
    for node in nodes:
        if node["type"] == "start": continue
        node_id = node["id"]
        d = node["data"]

        title = d.get("title", "제목 없음")
        desc = d.get("description", "")
        bg = d.get("background", "")
        trigger = d.get("trigger", "")
        rule = d.get("rule", "")
        scene_type = d.get("scene_type", "normal")
        enemies = d.get("enemies", [])  # List[dict] or List[str]

        type_str = "전투(Battle)" if scene_type == "battle" else "일반(Normal)"

        blueprint += f"ID: {node_id} ({node['type']})\n유형: {type_str}\n제목: {title}\n"
        if bg: blueprint += f"배경: {bg}\n"
        if desc: blueprint += f"내용: {desc}\n"
        if trigger: blueprint += f"트리거: {trigger}\n"
        if rule: blueprint += f"추가 룰: {rule}\n"
        if enemies: blueprint += f"등장 적: {', '.join([e.get('name', str(e)) for e in enemies]) if isinstance(enemies, list) else str(enemies)}\n"

        outgoing = [e for e in edges if e["source"] == node_id]
        if outgoing:
            blueprint += "연결:\n"
            for e in outgoing:
                blueprint += f"  -> 목적지: {e['target']}\n"
        blueprint += "---\n"

    return {"blueprint": blueprint}


def refine_scenario_info(state: BuilderState):
    report_progress("building", "2/5", "개요 및 설정 기획 중...", 30, phase="worldbuilding")
    llm = LLMFactory.get_llm(state.get("model_name"))
    parser = JsonOutputParser(pydantic_object=ScenarioSummary)

    prompt = ChatPromptTemplate.from_messages([
        ("system",
         "당신은 TRPG 시나리오 작가입니다. JSON 형식으로 응답하세요.\n"
         "설계도(Blueprint)를 바탕으로 시나리오의 전체적인 개요를 작성해주세요.\n"
         "이미 작성된 프롤로그나 설정이 있다면 이를 존중하되, 더 풍부하게 다듬어주세요.\n"
         "{format_instructions}"),
        ("user", "{blueprint}")
    ])

    try:
        res = (prompt | llm | parser).invoke({
            "blueprint": state["blueprint"],
            "format_instructions": parser.get_format_instructions()
        })
        return {"scenario": res}
    except Exception as e:
        logger.error(f"Refine Error: {e}")
        return {"scenario": {"title": "Untitled", "summary": "", "player_prologue": "", "gm_notes": ""}}


def generate_full_content(state: BuilderState):
    report_progress("building", "3/5", "세계관 및 NPC 생성 중...", 50, phase="worldbuilding")
    llm = LLMFactory.get_llm(state.get("model_name"))
    blueprint = state.get("blueprint", "")

    # 1. NPC 및 World 생성
    npc_parser = JsonOutputParser(pydantic_object=NPCList)
    npc_chain = (
            ChatPromptTemplate.from_messages([
                ("system", "NPC 상세 설정을 생성하세요. 설계도에 없는 NPC는 추가하지 마세요.\n{format_instructions}"),
                ("user", "{blueprint}")
            ]).partial(format_instructions=npc_parser.get_format_instructions())
            | llm | npc_parser
    )

    world_parser = JsonOutputParser(pydantic_object=WorldList)
    world_chain = (
            ChatPromptTemplate.from_messages([
                ("system", "배경 장소 3곳을 묘사하세요.\n{format_instructions}"),
                ("user", "{blueprint}")
            ]).partial(format_instructions=world_parser.get_format_instructions())
            | llm | world_parser
    )

    setup_chain = RunnableParallel(npcs=npc_chain, worlds=world_chain)

    try:
        setup_res = setup_chain.invoke({"blueprint": blueprint})
    except Exception as e:
        logger.error(f"Setup Gen Error: {e}")
        setup_res = {"npcs": {"npcs": []}, "worlds": {"worlds": []}}

    npcs = setup_res['npcs'].get('npcs', [])
    worlds = setup_res['worlds'].get('worlds', [])

    report_progress("building", "3.5/5", "장면 및 사건 구성 중...", 65, phase="scene_generation")

    world_context = "\n".join([f"- {w.get('name')}: {w.get('description')}" for w in worlds])
    npc_context = "\n".join([f"- {n.get('name')}: {n.get('role')}" for n in npcs])

    # 2. Scene 생성
    scene_parser = JsonOutputParser(pydantic_object=SceneData)

    scene_prompt = ChatPromptTemplate.from_messages([
        ("system",
         "설계도를 바탕으로 씬 데이터를 생성하세요. ID는 절대 변경하지 마세요.\n"
         "각 장면의 유형(Normal/Battle)에 맞춰 서술하세요.\n"
         "사용자가 입력한 트리거, 배경, 룰, 적 정보가 있다면 반드시 포함하세요.\n"
         "{format_instructions}"),
        ("user",
         f"설계도:\n{blueprint}\n\n"
         f"세계관:\n{world_context}\n\n"
         f"NPC:\n{npc_context}")
    ]).partial(format_instructions=scene_parser.get_format_instructions())

    scene_chain = scene_prompt | llm | scene_parser

    try:
        content = scene_chain.invoke({"blueprint": blueprint})
        return {
            "characters": npcs,
            "worlds": worlds,
            "scenes": content.get('scenes', []),
            "endings": content.get('endings', [])
        }
    except Exception as e:
        logger.error(f"Scene Gen Error: {e}")
        return {"characters": npcs, "worlds": worlds, "scenes": [], "endings": []}


class InitialStateExtractor(BaseModel):
    hp: Optional[int] = Field(None, description="체력")
    mp: Optional[int] = Field(None, description="마력")
    sanity: Optional[int] = Field(None, description="정신력")
    gold: Optional[int] = Field(None, description="골드")
    inventory: Optional[List[str]] = Field(None, description="아이템")


def finalize_build(state: BuilderState):
    report_progress("building", "5/5", "최종 마무리 중...", 100, phase="finalizing")
    data = state["graph_data"]

    # 1. 시작점 연결
    start_id = None
    start_node = next((n for n in data.get("nodes", []) if n["type"] == "start"), None)
    if start_node:
        edge = next((e for e in data.get("edges", []) if e["source"] == start_node["id"]), None)
        if edge: start_id = edge["target"]
    if not start_id and state["scenes"]:
        start_id = state["scenes"][0]["scene_id"]

    prologue_connects = []
    if start_node:
        for edge in data.get("edges", []):
            if edge["source"] == start_node["id"]:
                prologue_connects.append(edge["target"])

    scenario_data = state.get("scenario", {})

    # 생성된 프롤로그/설정과 사용자 입력값 병합 (사용자 입력 우선 or 보완)
    # 여기서는 생성된 값을 우선하되, 없으면 raw 데이터 사용
    final_prologue = scenario_data.get("player_prologue") or start_node.get("data", {}).get("prologue", "")
    final_hidden = scenario_data.get("gm_notes") or start_node.get("data", {}).get("gm_notes", "")

    # 2. Raw Graph 업데이트 (프론트엔드 동기화용)
    raw_nodes = state["graph_data"].get("nodes", [])
    scene_map = {s["scene_id"].lower(): s for s in state["scenes"]}
    ending_map = {e["ending_id"].lower(): e for e in state["endings"]}

    for node in raw_nodes:
        nid = node["id"].lower()
        if nid in scene_map:
            tgt = scene_map[nid]
            node["data"]["title"] = tgt["name"]
            node["data"]["description"] = tgt["description"]
            node["data"]["npcs"] = tgt["npcs"]
            # 추가된 필드 반영
            if "background" in tgt: node["data"]["background"] = tgt["background"]
            if "trigger" in tgt: node["data"]["trigger"] = tgt["trigger"]
            if "rule" in tgt: node["data"]["rule"] = tgt["rule"]
            if "enemies" in tgt: node["data"]["enemies"] = tgt["enemies"]

        elif nid in ending_map:
            tgt = ending_map[nid]
            node["data"]["title"] = tgt["title"]
            node["data"]["description"] = tgt["description"]
            if "background" in tgt: node["data"]["background"] = tgt["background"]

    # 3. 초기 스탯 추출
    initial_player_state = {"hp": 100, "inventory": []}

    try:
        extract_llm = LLMFactory.get_llm(state.get("model_name"), temperature=0.1)
        parser = JsonOutputParser(pydantic_object=InitialStateExtractor)
        extract_prompt = ChatPromptTemplate.from_messages([
            ("system", "GM 노트를 분석하여 시작 스탯을 추출하세요.\n{format_instructions}"),
            ("user", f"{final_hidden}")
        ]).partial(format_instructions=parser.get_format_instructions())

        extracted_stats = (extract_prompt | extract_llm | parser).invoke({})
        if extracted_stats:
            for k, v in extracted_stats.items():
                if v is not None: initial_player_state[k] = v
    except Exception as e:
        logger.warning(f"Stats Extraction Failed: {e}")

    final_data = {
        "title": scenario_data.get("title", "Untitled"),
        "desc": scenario_data.get("summary", ""),
        "prologue": final_prologue,
        "world_settings": final_hidden,
        "player_status": final_hidden,
        "prologue_connects_to": prologue_connects,
        "scenario": scenario_data,
        "worlds": state["worlds"],
        "npcs": state["characters"],
        "scenes": state["scenes"],
        "endings": state["endings"],
        "start_scene_id": start_id,
        "initial_state": initial_player_state,
        "raw_graph": state["graph_data"]
    }

    final_data = renumber_scenes_bfs(final_data)
    return {"final_data": final_data}


def build_builder_graph():
    workflow = StateGraph(BuilderState)
    workflow.add_node("validate", validate_structure)
    workflow.add_node("parse", parse_graph_to_blueprint)
    workflow.add_node("refine", refine_scenario_info)
    workflow.add_node("generate", generate_full_content)
    workflow.add_node("finalize", finalize_build)

    workflow.set_entry_point("validate")
    workflow.add_edge("validate", "parse")
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
    return app.invoke(initial_state)['final_data']


def generate_single_npc(scenario_title, scenario_summary, user_request="", model_name=None):
    # 단일 NPC 생성은 npc_generator.html 내부 API에서 처리될 수도 있으나,
    # 호환성을 위해 유지
    llm = LLMFactory.get_llm(model_name)
    parser = JsonOutputParser(pydantic_object=NPC)
    prompt = ChatPromptTemplate.from_messages([
        ("system", "캐릭터 생성기\n{format_instructions}"),
        ("user", f"제목:{scenario_title}\n요청:{user_request}")
    ]).partial(format_instructions=parser.get_format_instructions())
    return (prompt | llm | parser).invoke({})