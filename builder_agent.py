import json
import os
import yaml
import logging
from typing import TypedDict, List, Annotated, Optional, Dict, Any, Callable
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.runnables import RunnableParallel
from langgraph.graph import StateGraph, END

from llm_factory import LLMFactory
from schemas import NPC
from core.utils import renumber_scenes_bfs

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# --- 프롬프트 로더 ---
def load_prompts(filepath: str = "config/prompts.yaml") -> Dict[str, str]:
    if not os.path.exists(filepath):
        alt_path = os.path.join(os.path.dirname(__file__), "prompts.yaml")
        if os.path.exists(alt_path):
            filepath = alt_path
        else:
            logger.warning(f"Prompts file not found at {filepath}. Using empty prompts.")
            return {}

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.error(f"Failed to load prompts: {e}")
        return {}


PROMPTS = load_prompts()

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


# --- [유틸리티] JSON 파싱 및 헬퍼 ---

def parse_json_garbage(text: str) -> dict:
    if isinstance(text, dict): return text
    if not text: return {}
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
        return {}


def safe_invoke_json(chain, input_data: dict, retries: int = 2, fallback: Any = None):
    for attempt in range(retries + 1):
        try:
            return chain.invoke(input_data)
        except Exception as e:
            logger.warning(f"LLM Invoke failed (Attempt {attempt + 1}/{retries + 1}): {e}")
            if attempt == retries:
                return fallback if fallback is not None else {}
    return fallback if fallback is not None else {}


# [최적화] 일반적인 요약 (World 등)
def summarize_context(items: List[Dict], key_name: str, key_desc: str, limit: int = 10) -> str:
    if not items: return "없음"

    summary_list = []
    count = len(items)
    target_items = items[:limit]

    for item in target_items:
        name = item.get(key_name, "Unknown")
        desc = item.get(key_desc, "")
        summary_list.append(f"- {name}: {desc}")

    if count > limit:
        summary_list.append(f"...외 {count - limit}개")

    return "\n".join(summary_list)


# [NEW] NPC 전용 스마트 요약 (이름, 역할, 외모, 성격만 추출)
def summarize_npc_context(npcs: List[Dict], limit: int = 15) -> str:
    if not npcs: return "없음"

    summary_list = []
    count = len(npcs)
    target_items = npcs[:limit]

    for npc in target_items:
        name = npc.get("name", "Unknown")
        role = npc.get("role") or npc.get("type") or "Unknown"

        # 묘사에 필수적인 요소만 압축해서 전달 (비밀, 스탯 등 제외)
        traits = []
        if npc.get("appearance"): traits.append(f"외모:{npc.get('appearance')}")
        if npc.get("personality"): traits.append(f"성격:{npc.get('personality')}")

        trait_str = f" ({', '.join(traits)})" if traits else ""
        summary_list.append(f"- {name} [{role}]{trait_str}")

    if count > limit:
        summary_list.append(f"...외 {count - limit}명")

    return "\n".join(summary_list)


# --- 데이터 모델 (Pydantic) ---

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


# --- 노드 타입별 검증 로직 ---

def validate_start_node(node: dict, edge_map: dict) -> None:
    data = node["data"]
    if not all([data.get(k) for k in ["label", "prologue", "gm_notes", "background"]]):
        logger.warning(f"Start node {node['id']} missing fields")

    # [수정] Start Node는 반드시 1개의 연결만 가져야 함
    out_edges = edge_map[node["id"]]["out"]
    if len(out_edges) == 0:
        raise ValueError("시작점(프롤로그)에 첫 번째 장면을 연결해주세요.")
    if len(out_edges) > 1:
        raise ValueError("시작점(프롤로그)은 오직 하나의 오프닝 장면과만 연결할 수 있습니다. 분기점은 그 이후 장면부터 만들어주세요.")


def validate_scene_node(node: dict, edge_map: dict) -> None:
    if not edge_map[node["id"]]["in"]:
        raise ValueError(f"'{node['data'].get('title')}' 장면으로 들어오는 연결이 없습니다.")

    if not edge_map[node["id"]]["out"]:
        raise ValueError(f"'{node['data'].get('title')}' 장면에서 다음으로 가는 연결이 없습니다.")


def validate_ending_node(node: dict, edge_map: dict) -> None:
    if not edge_map[node["id"]]["in"]:
        raise ValueError(f"'{node['data'].get('title')}' 엔딩으로 들어오는 연결이 없습니다.")


NODE_VALIDATORS: Dict[str, Callable] = {
    "start": validate_start_node,
    "scene": validate_scene_node,
    "ending": validate_ending_node
}


# --- 노드 함수 (LangGraph Nodes) ---

def validate_structure(state: BuilderState):
    logger.info("Validating graph structure...")
    report_progress("building", "0/5", "구조 및 연결 검증 중...", 5, phase="initializing")

    graph_data = state["graph_data"]
    nodes = graph_data.get("nodes", [])
    edges = graph_data.get("edges", [])

    edge_map = {n["id"]: {"in": [], "out": []} for n in nodes}
    for edge in edges:
        src, tgt = edge["source"], edge["target"]
        if src in edge_map: edge_map[src]["out"].append(tgt)
        if tgt in edge_map: edge_map[tgt]["in"].append(src)

    for node in nodes:
        ntype = node["type"]
        validator = NODE_VALIDATORS.get(ntype)
        if validator:
            validator(node, edge_map)
        else:
            logger.warning(f"Unknown node type: {ntype}")

    return state


def parse_graph_to_blueprint(state: BuilderState):
    """
    Blueprint 생성 단계: 여기에는 모든 상세 정보를 다 적음 (마스터 계획서니까)
    """
    report_progress("building", "1/5", "구조 분석 중...", 10, phase="parsing")
    data = state["graph_data"]
    nodes = data.get("nodes", [])
    edges = data.get("edges", [])
    raw_npcs = data.get("npcs", [])

    blueprint = "### 시나리오 구조 명세서 ###\n\n"

    start_node = next((n for n in nodes if n["type"] == "start"), None)
    if start_node:
        d = start_node['data']
        blueprint += f"[설정]\n제목: {d.get('label', '')}\n"
        blueprint += f"프롤로그: {d.get('prologue', '')}\n"
        blueprint += f"시스템 설정: {d.get('gm_notes', '')}\n"
        blueprint += f"배경 묘사: {d.get('background', '')}\n\n"

    blueprint += "[등장인물 및 적 상세]\n"
    for npc in raw_npcs:
        name = npc.get('name', 'Unknown')
        role = npc.get('role') or npc.get('type') or 'Unknown'

        # 상세 정보 풀버전 (Blueprint용)
        desc_parts = []
        if npc.get('personality'): desc_parts.append(f"성격: {npc.get('personality')}")
        if npc.get('appearance'): desc_parts.append(f"외모: {npc.get('appearance')}")
        if npc.get('dialogue'): desc_parts.append(f"대표 대사: \"{npc.get('dialogue')}\"")
        if npc.get('secret'): desc_parts.append(f"비밀: {npc.get('secret')}")

        if npc.get('isEnemy'):
            stats = []
            if npc.get('hp'): stats.append(f"HP {npc.get('hp')}")
            if npc.get('attack'): stats.append(f"ATK {npc.get('attack')}")
            if npc.get('weakness'): stats.append(f"약점: {npc.get('weakness')}")
            if stats: desc_parts.append(f"전투: {', '.join(stats)}")

        desc_str = " / ".join(desc_parts)
        blueprint += f"- {name} ({role}): {desc_str}\n"
        if npc.get('description'):
            blueprint += f"  배경설명: {npc.get('description')}\n"
    blueprint += "\n"

    blueprint += "[장면 흐름]\n"
    for node in nodes:
        if node["type"] == "start": continue
        d = node["data"]

        blueprint += f"ID: {node['id']} ({node['type']})\n"
        blueprint += f"제목: {d.get('title', '제목 없음')}\n"
        blueprint += f"유형: {d.get('scene_type', 'normal')}\n"
        if d.get('background'): blueprint += f"배경: {d.get('background')}\n"
        if d.get('description'): blueprint += f"내용: {d.get('description')}\n"
        if d.get('trigger'): blueprint += f"트리거: {d.get('trigger')}\n"
        if d.get('rule'): blueprint += f"추가 룰: {d.get('rule')}\n"

        enemies = d.get("enemies", [])
        if enemies:
            e_str = ', '.join([e.get('name', str(e)) for e in enemies]) if isinstance(enemies, list) else str(enemies)
            blueprint += f"등장 적: {e_str}\n"

        outgoing = [e for e in edges if e["source"] == node["id"]]
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

    prompt_text = PROMPTS.get("refine_scenario", "기본 프롬프트...")
    prompt = ChatPromptTemplate.from_messages([
        ("system", prompt_text),
        ("user", "{blueprint}")
    ]).partial(format_instructions=parser.get_format_instructions())

    res = safe_invoke_json(
        prompt | llm | parser,
        {"blueprint": state["blueprint"]},
        retries=2,
        fallback={"title": "Untitled", "summary": "", "player_prologue": "", "gm_notes": ""}
    )
    return {"scenario": res}


def generate_full_content(state: BuilderState):
    report_progress("building", "3/5", "세계관 및 NPC 생성 중...", 50, phase="worldbuilding")
    llm = LLMFactory.get_llm(state.get("model_name"))
    blueprint = state.get("blueprint", "")

    npc_parser = JsonOutputParser(pydantic_object=NPCList)
    npc_chain = (
            ChatPromptTemplate.from_messages([
                ("system", PROMPTS.get("generate_npc", "")),
                ("user", "{blueprint}")
            ]).partial(format_instructions=npc_parser.get_format_instructions())
            | llm | npc_parser
    )

    world_parser = JsonOutputParser(pydantic_object=WorldList)
    world_chain = (
            ChatPromptTemplate.from_messages([
                ("system", PROMPTS.get("generate_world", "")),
                ("user", "{blueprint}")
            ]).partial(format_instructions=world_parser.get_format_instructions())
            | llm | world_parser
    )

    try:
        setup_res = RunnableParallel(npcs=npc_chain, worlds=world_chain).invoke({"blueprint": blueprint})
    except Exception as e:
        logger.error(f"Setup Gen Error: {e}")
        setup_res = {"npcs": {"npcs": []}, "worlds": {"worlds": []}}

    npcs = setup_res['npcs'].get('npcs', [])
    worlds = setup_res['worlds'].get('worlds', [])

    report_progress("building", "3.5/5", "장면 및 사건 구성 중...", 65, phase="scene_generation")

    # [최적화 적용] 씬 생성용 컨텍스트는 스마트 요약 사용 (토큰 절약 + 핵심 정보 전달)
    world_context = summarize_context(worlds, 'name', 'description', limit=5)
    # 여기서 summarize_npc_context 사용!
    npc_context = summarize_npc_context(state["graph_data"].get("npcs", []), limit=20)

    scene_parser = JsonOutputParser(pydantic_object=SceneData)
    scene_prompt = ChatPromptTemplate.from_messages([
        ("system", PROMPTS.get("generate_scene", "")),
        ("user", f"설계도:\n{blueprint}\n\n세계관:\n{world_context}\n\nNPC:\n{npc_context}")
    ]).partial(format_instructions=scene_parser.get_format_instructions())

    content = safe_invoke_json(
        scene_prompt | llm | scene_parser,
        {},
        retries=2,
        fallback={"scenes": [], "endings": []}
    )

    return {
        "characters": npcs,
        "worlds": worlds,
        "scenes": content.get('scenes', []),
        "endings": content.get('endings', [])
    }


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
    start_node = next((n for n in data.get("nodes", []) if n["type"] == "start"), None)

    start_id = None
    prologue_connects = []

    if start_node:
        for edge in data.get("edges", []):
            if edge["source"] == start_node["id"]:
                prologue_connects.append(edge["target"])
                if not start_id: start_id = edge["target"]

    scenario_data = state.get("scenario", {})
    final_prologue = scenario_data.get("player_prologue") or start_node.get("data", {}).get("prologue", "")
    final_hidden = scenario_data.get("gm_notes") or start_node.get("data", {}).get("gm_notes", "")

    # 2. Raw Graph 업데이트
    raw_nodes = state["graph_data"].get("nodes", [])
    scene_map = {s["scene_id"].lower(): s for s in state["scenes"]}
    ending_map = {e["ending_id"].lower(): e for e in state["endings"]}

    for node in raw_nodes:
        nid = node["id"].lower()
        if nid in scene_map:
            tgt = scene_map[nid]
            node["data"].update({
                "title": tgt["name"],
                "description": tgt["description"],
                "npcs": tgt["npcs"],
                "background": tgt.get("background"),
                "trigger": tgt.get("trigger"),
                "rule": tgt.get("rule"),
                "enemies": tgt.get("enemies")
            })
        elif nid in ending_map:
            tgt = ending_map[nid]
            node["data"].update({
                "title": tgt["title"],
                "description": tgt["description"],
                "background": tgt.get("background")
            })

    # 3. 초기 스탯 설정 (명시적 값 우선 사용)
    initial_player_state = {"hp": 100, "inventory": []}

    if start_node:
        d = start_node.get("data", {})
        if "initial_hp" in d: initial_player_state["hp"] = d["initial_hp"]
        if "initial_items" in d:
            items = d["initial_items"]
            if isinstance(items, str) and items.strip():
                initial_player_state["inventory"] = [i.strip() for i in items.split(',')]
            elif isinstance(items, list):
                initial_player_state["inventory"] = items

        custom_stats = d.get("custom_stats", [])
        stat_rules = d.get("stat_rules", "")

        custom_stats_text = []
        for stat in custom_stats:
            name = stat.get("name")
            val = stat.get("value")
            if name:
                initial_player_state[name] = val
                custom_stats_text.append(f"{name}: {val}")

        append_text = ""
        if custom_stats_text:
            append_text += "\n\n[추가 스탯 설정]\n" + "\n".join(custom_stats_text)
        if stat_rules:
            append_text += "\n\n[스탯 규칙]\n" + stat_rules

        final_hidden += append_text

    extract_llm = LLMFactory.get_llm(state.get("model_name"), temperature=0.0)
    parser = JsonOutputParser(pydantic_object=InitialStateExtractor)

    extract_prompt = ChatPromptTemplate.from_messages([
        ("system", PROMPTS.get("extract_stats", "")),
        ("user", "{gm_notes}")
    ]).partial(format_instructions=parser.get_format_instructions())

    extracted_stats = safe_invoke_json(
        extract_prompt | extract_llm | parser,
        {"gm_notes": final_hidden},
        retries=2,
        fallback={}
    )

    for k, v in extracted_stats.items():
        if v is not None and k not in initial_player_state:
            initial_player_state[k] = v

    # 4. NPC 데이터 병합 (유저 입력 우선)
    final_npcs = state["characters"]
    user_npcs = {n.get("name"): n for n in state["graph_data"].get("npcs", [])}

    for npc in final_npcs:
        u_npc = user_npcs.get(npc["name"])
        if u_npc:
            npc.update(u_npc)

    existing_names = {n["name"] for n in final_npcs}
    for u_npc in state["graph_data"].get("npcs", []):
        if u_npc.get("name") not in existing_names:
            final_npcs.append(u_npc)

    final_data = {
        "title": scenario_data.get("title", "Untitled"),
        "desc": scenario_data.get("summary", ""),
        "prologue": final_prologue,
        "world_settings": final_hidden,
        "player_status": final_hidden,
        "prologue_connects_to": prologue_connects,
        "scenario": scenario_data,
        "worlds": state["worlds"],
        "npcs": final_npcs,
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
    llm = LLMFactory.get_llm(model_name)
    parser = JsonOutputParser(pydantic_object=NPC)

    prompt = ChatPromptTemplate.from_messages([
        ("system", PROMPTS.get("generate_single_npc", "")),
        ("user", f"제목:{scenario_title}\n요청:{user_request}")
    ]).partial(format_instructions=parser.get_format_instructions())

    return safe_invoke_json(prompt | llm | parser, {}, retries=1)