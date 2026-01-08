import json
import os
import yaml
import logging
import concurrent.futures
from typing import TypedDict, List, Annotated, Optional, Dict, Any, Callable
from collections import deque

from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.runnables import RunnableParallel
from langgraph.graph import StateGraph, END

from llm_factory import LLMFactory
from schemas import NPC
# core.utils의 renumber_scenes_bfs는 이제 사용하지 않으므로 임포트에서 제거해도 되지만,
# 호환성을 위해 남겨두거나 사용하지 않음 처리합니다.
from core.utils import renumber_scenes_bfs

# [NEW] 검수 서비스 임포트
try:
    from services.ai_audit_service import AiAuditService
except ImportError:
    class AiAuditService:
        @staticmethod
        def audit_scenario(data):
            return {"valid": True, "score": 0, "feedback": ["검수 모듈을 찾을 수 없습니다."]}

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# --- 프롬프트 로더 ---
def load_prompts() -> Dict[str, str]:
    base_dir = os.path.dirname(__file__)
    possible_paths = [
        os.path.join(base_dir, "config", "prompt.yaml"),
        os.path.join(base_dir, "config", "prompts.yaml"),
        os.path.join(base_dir, "prompt.yaml"),
        "config/prompt.yaml",
        "config/prompts.yaml"
    ]

    for path in possible_paths:
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f)
                    if isinstance(data, dict):
                        logger.info(f"Loaded prompts from {path}")
                        return data
                    else:
                        logger.warning(f"Prompts file at {path} is not a dictionary. Returning empty.")
            except Exception as e:
                logger.error(f"Failed to load prompts from {path}: {e}")

    logger.warning("Prompts file not found in any standard location. Using empty prompts.")
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


def summarize_context(items: List[Dict], key_name: str, key_desc: str, limit: int = 10) -> str:
    if not items: return "없음"
    summary_list = []
    count = len(items)
    target_items = items[:limit]

    for item in target_items:
        if not isinstance(item, dict): continue
        name = item.get(key_name, "Unknown")
        desc = item.get(key_desc, "")
        summary_list.append(f"- {name}: {desc}")

    if count > limit:
        summary_list.append(f"...외 {count - limit}개")
    return "\n".join(summary_list)


def summarize_npc_context(npcs: List[Dict], limit: int = 15) -> str:
    if not npcs: return "없음"
    summary_list = []
    count = len(npcs)
    target_items = npcs[:limit]

    for npc in target_items:
        if not isinstance(npc, dict): continue
        name = npc.get("name", "Unknown")
        role = npc.get("role") or npc.get("type") or "Unknown"
        traits = []
        if npc.get("appearance"): traits.append(f"외모:{npc.get('appearance')}")
        if npc.get("personality"): traits.append(f"성격:{npc.get('personality')}")
        if npc.get("dialogue_style"): traits.append(f"말투:{npc.get('dialogue_style')}")

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
    data = node.get("data", {})
    if not isinstance(data, dict): data = {}
    if not all([data.get(k) for k in ["label", "prologue", "gm_notes", "background"]]):
        logger.warning(f"Start node {node.get('id')} missing fields")
    out_edges = edge_map[node["id"]]["out"]
    if len(out_edges) == 0:
        raise ValueError("시작점(프롤로그)에 첫 번째 장면을 연결해주세요.")
    if len(out_edges) > 1:
        raise ValueError("시작점(프롤로그)은 오직 하나의 오프닝 장면과만 연결할 수 있습니다.")


def validate_scene_node(node: dict, edge_map: dict) -> None:
    data = node.get("data", {})
    if not isinstance(data, dict): data = {}
    if not edge_map[node["id"]]["in"]:
        raise ValueError(f"'{data.get('title')}' 장면으로 들어오는 연결이 없습니다.")
    if not edge_map[node["id"]]["out"]:
        raise ValueError(f"'{data.get('title')}' 장면에서 다음으로 가는 연결이 없습니다.")


def validate_ending_node(node: dict, edge_map: dict) -> None:
    data = node.get("data", {})
    if not isinstance(data, dict): data = {}
    if not edge_map[node["id"]]["in"]:
        raise ValueError(f"'{data.get('title')}' 엔딩으로 들어오는 연결이 없습니다.")


NODE_VALIDATORS: Dict[str, Callable] = {
    "start": validate_start_node,
    "scene": validate_scene_node,
    "ending": validate_ending_node
}


# --- 노드 함수 ---

def validate_structure(state: BuilderState):
    logger.info("Validating graph structure...")
    report_progress("building", "0/5", "구조 및 연결 검증 중...", 5, phase="initializing")

    graph_data = state["graph_data"]
    if isinstance(graph_data, str):
        try:
            graph_data = json.loads(graph_data)
        except:
            pass
    if not isinstance(graph_data, dict):
        raise ValueError("입력 데이터가 올바른 JSON 형식이 아닙니다.")

    # 노드 파싱
    raw_nodes = graph_data.get("nodes", [])
    valid_nodes = []
    if isinstance(raw_nodes, list):
        for node in raw_nodes:
            if isinstance(node, str):
                try:
                    node = json.loads(node)
                except:
                    continue
            if not isinstance(node, dict): continue
            if "data" in node and isinstance(node["data"], str):
                try:
                    node["data"] = json.loads(node["data"])
                except:
                    node["data"] = {}
            if "data" not in node or not isinstance(node["data"], dict):
                node["data"] = {}
            valid_nodes.append(node)
    graph_data["nodes"] = valid_nodes

    # 엣지 파싱
    raw_edges = graph_data.get("edges", [])
    valid_edges = []
    if isinstance(raw_edges, list):
        for edge in raw_edges:
            if isinstance(edge, str):
                try:
                    edge = json.loads(edge)
                except:
                    continue
            if isinstance(edge, dict):
                valid_edges.append(edge)
    graph_data["edges"] = valid_edges
    state["graph_data"] = graph_data

    nodes = graph_data.get("nodes", [])
    edge_map = {n.get("id"): {"in": [], "out": []} for n in nodes if isinstance(n, dict) and "id" in n}

    for edge in valid_edges:
        src, tgt = edge.get("source"), edge.get("target")
        if src in edge_map: edge_map[src]["out"].append(tgt)
        if tgt in edge_map: edge_map[tgt]["in"].append(src)

    for node in nodes:
        ntype = node.get("type", "unknown")
        validator = NODE_VALIDATORS.get(ntype)
        if validator: validator(node, edge_map)

    return state


def parse_graph_to_blueprint(state: BuilderState):
    report_progress("building", "1/5", "구조 분석 중...", 10, phase="parsing")
    data = state["graph_data"]
    nodes = data.get("nodes", [])
    edges = data.get("edges", [])
    raw_npcs = data.get("npcs", [])

    blueprint = "### 시나리오 구조 명세서 ###\n\n"
    start_node = next((n for n in nodes if n.get("type") == "start"), None)
    if start_node:
        d = start_node.get('data', {})
        blueprint += f"[설정]\n제목: {d.get('label', '')}\n프롤로그: {d.get('prologue', '')}\n"
        blueprint += f"시스템 설정: {d.get('gm_notes', '')}\n배경 묘사: {d.get('background', '')}\n\n"

    blueprint += "[등장인물 및 적 상세]\n"
    if isinstance(raw_npcs, list):
        for npc in raw_npcs:
            if not isinstance(npc, dict): continue
            name = npc.get('name', 'Unknown')
            role = npc.get('role') or npc.get('type') or 'Unknown'
            desc_parts = []
            if npc.get('personality'): desc_parts.append(f"성격: {npc.get('personality')}")
            if npc.get('appearance'): desc_parts.append(f"외모: {npc.get('appearance')}")
            if npc.get('dialogue'): desc_parts.append(f"대사: \"{npc.get('dialogue')}\"")
            if npc.get('secret'): desc_parts.append(f"비밀: {npc.get('secret')}")
            if npc.get('isEnemy'):
                stats = []
                if npc.get('hp'): stats.append(f"HP {npc.get('hp')}")
                if npc.get('attack'): stats.append(f"ATK {npc.get('attack')}")
                if npc.get('weakness'): stats.append(f"약점: {npc.get('weakness')}")
                if stats: desc_parts.append(f"전투: {', '.join(stats)}")
            blueprint += f"- {name} ({role}): {' / '.join(desc_parts)}\n"
            if npc.get('description'): blueprint += f"  설명: {npc.get('description')}\n"
    blueprint += "\n[장면 흐름]\n"

    for node in nodes:
        if node.get("type") == "start": continue
        d = node.get("data", {})
        blueprint += f"ID: {node.get('id')} ({node.get('type')})\n제목: {d.get('title', '제목 없음')}\n"
        blueprint += f"유형: {d.get('scene_type', 'normal')}\n"
        if d.get('background'): blueprint += f"배경: {d.get('background')}\n"
        if d.get('description'): blueprint += f"내용: {d.get('description')}\n"
        if d.get('trigger'): blueprint += f"트리거: {d.get('trigger')}\n"

        enemies = d.get("enemies", [])
        if enemies:
            e_str = ', '.join([e.get('name', 'Unknown') if isinstance(e, dict) else str(e) for e in enemies])
            blueprint += f"등장 적: {e_str}\n"

        scene_npcs = d.get("npcs", [])
        if scene_npcs:
            n_str = ', '.join([n.get('name', 'Unknown') if isinstance(n, dict) else str(n) for n in scene_npcs])
            blueprint += f"등장 NPC: {n_str}\n"

        outgoing = [e for e in edges if e.get("source") == node.get("id")]
        if outgoing:
            blueprint += "연결:\n"
            for e in outgoing:
                blueprint += f"  -> 목적지: {e.get('target')}\n"
        blueprint += "---\n"

    return {"blueprint": blueprint}


def refine_scenario_info(state: BuilderState):
    report_progress("building", "2/5", "개요 및 설정 기획 중...", 30, phase="worldbuilding")
    llm = LLMFactory.get_llm(state.get("model_name"))
    parser = JsonOutputParser(pydantic_object=ScenarioSummary)
    prompt = ChatPromptTemplate.from_messages([
        ("system", PROMPTS.get("refine_scenario", "Refine scenario summary.")),
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
                ("system", PROMPTS.get("generate_npc", "Generate NPCs.")),
                ("user", "{blueprint}")
            ]).partial(format_instructions=npc_parser.get_format_instructions())
            | llm | npc_parser
    )

    world_parser = JsonOutputParser(pydantic_object=WorldList)
    world_chain = (
            ChatPromptTemplate.from_messages([
                ("system", PROMPTS.get("generate_world", "Generate world.")),
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
    world_context = summarize_context(worlds, 'name', 'description', limit=10)

    generated_npcs_map = {n['name']: n for n in npcs}
    graph_npcs = state["graph_data"].get("npcs", [])
    merged_npcs = []

    if isinstance(graph_npcs, list):
        for g_npc in graph_npcs:
            if not isinstance(g_npc, dict): continue
            name = g_npc.get("name")
            if name in generated_npcs_map:
                merged_npcs.append(generated_npcs_map[name])
            else:
                merged_npcs.append(g_npc)

    existing_names = {n.get("name") for n in merged_npcs}
    for n in npcs:
        if n.get("name") not in existing_names:
            merged_npcs.append(n)

    npc_context = summarize_npc_context(merged_npcs, limit=20)

    scene_parser = JsonOutputParser(pydantic_object=SceneData)
    scene_prompt = ChatPromptTemplate.from_messages([
        ("system", PROMPTS.get("generate_scene", "Generate scenes.")),
        ("user", f"설계도:\n{blueprint}\n\n[참고: 세계관]\n{world_context}\n\n[참고: NPC]\n{npc_context}")
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


def parallel_generation_node(state: BuilderState):
    report_progress("building", "2/5", "시나리오 개요 및 상세 콘텐츠 동시 생성 중...", 40, phase="parallel_gen")
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future_refine = executor.submit(refine_scenario_info, state)
        future_generate = executor.submit(generate_full_content, state)
        try:
            refine_result = future_refine.result()
            generate_result = future_generate.result()
        except Exception as e:
            logger.error(f"Parallel Generation Error: {e}")
            raise e
    return {**refine_result, **generate_result}


class InitialStateExtractor(BaseModel):
    hp: Optional[int] = Field(None, description="체력")
    mp: Optional[int] = Field(None, description="마력")
    sanity: Optional[int] = Field(None, description="정신력")
    gold: Optional[int] = Field(None, description="골드")
    inventory: Optional[List[str]] = Field(None, description="아이템")


def finalize_build(state: BuilderState):
    """
    최종 단계에서 직접 BFS 탐색을 수행하여
    Scene과 Ending을 명확히 구분하고 ID를 Scene-N, Ending-N으로 재할당합니다.
    """
    report_progress("building", "4/5", "데이터 통합 및 최종 마무리 중...", 90, phase="finalizing")

    graph_data = state["graph_data"]
    raw_nodes = graph_data.get("nodes", [])
    raw_edges = graph_data.get("edges", [])

    # 1. Start 노드 찾기
    start_node = next((n for n in raw_nodes if n.get("type") == "start"), None)
    if not start_node:
        raise ValueError("Start Node not found")

    # 2. Prologue 및 초기 설정 처리
    scenario_data = state.get("scenario", {})
    start_data = start_node.get("data", {})
    if not isinstance(start_data, dict): start_data = {}

    final_prologue = scenario_data.get("player_prologue") or start_data.get("prologue", "")
    final_hidden = scenario_data.get("gm_notes") or start_data.get("gm_notes", "")

    # 초기 스탯 추출
    initial_player_state = {"hp": 100, "inventory": []}
    if "initial_hp" in start_data: initial_player_state["hp"] = start_data["initial_hp"]
    if "initial_items" in start_data:
        items = start_data["initial_items"]
        if isinstance(items, str) and items.strip():
            initial_player_state["inventory"] = [i.strip() for i in items.split(',')]
        elif isinstance(items, list):
            initial_player_state["inventory"] = items

    custom_stats = start_data.get("custom_stats", [])
    stat_rules = start_data.get("stat_rules", "")
    custom_stats_text = []
    if isinstance(custom_stats, list):
        for stat in custom_stats:
            if isinstance(stat, dict) and stat.get("name"):
                initial_player_state[stat["name"]] = stat.get("value")
                custom_stats_text.append(f"{stat['name']}: {stat.get('value')}")

    if custom_stats_text: final_hidden += "\n\n[추가 스탯 설정]\n" + "\n".join(custom_stats_text)
    if stat_rules: final_hidden += "\n\n[스탯 규칙]\n" + str(stat_rules)

    # LLM으로 스탯 추가 추출
    extract_llm = LLMFactory.get_llm(state.get("model_name"), temperature=0.0)
    parser = JsonOutputParser(pydantic_object=InitialStateExtractor)
    extract_prompt = ChatPromptTemplate.from_messages([
        ("system", PROMPTS.get("extract_stats", "Extract stats.")),
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

    # --- [핵심 수정] BFS 기반 리넘버링 및 Scene/Ending 분리 ---

    # 3. AI가 생성한 Scene/Ending 데이터 매핑 (ID는 lowercase로 비교)
    generated_scene_map = {s["scene_id"].lower(): s for s in state["scenes"]}
    generated_ending_map = {e["ending_id"].lower(): e for e in state["endings"]}

    # 그래프 구조 파악용 인접 리스트
    adj_list = {n["id"]: [] for n in raw_nodes}
    for edge in raw_edges:
        src, tgt = edge.get("source"), edge.get("target")
        if src in adj_list and tgt in adj_list:
            adj_list[src].append(tgt)

    # Start에서 연결된 첫 번째 노드 찾기
    start_neighbors = adj_list.get(start_node["id"], [])
    if not start_neighbors:
        raise ValueError("Start node has no outgoing connection")

    # BFS 탐색 준비
    queue = deque(start_neighbors)  # Start 노드 다음부터 시작
    visited = set(start_neighbors)

    id_map = {}  # Old ID -> New ID
    final_scenes = []
    final_endings = []

    scene_counter = 1
    ending_counter = 1

    # 탐색 루프
    while queue:
        curr_id = queue.popleft()
        curr_node = next((n for n in raw_nodes if n["id"] == curr_id), None)
        if not curr_node: continue

        node_type = curr_node.get("type", "scene")
        curr_id_lower = curr_id.lower()
        new_id = ""

        # A. 씬(Scene)인 경우
        if node_type == "scene":
            new_id = f"Scene-{scene_counter}"
            scene_counter += 1
            id_map[curr_id] = new_id

            # 데이터 병합 (AI 생성 데이터 + 유저 그래프 데이터)
            base_data = generated_scene_map.get(curr_id_lower, {})
            user_data = curr_node.get("data", {})

            merged_scene = {
                "scene_id": new_id,
                "name": user_data.get("title") or base_data.get("name") or f"Scene {scene_counter - 1}",
                "description": base_data.get("description") or user_data.get("description") or "내용 없음",
                "type": user_data.get("scene_type") or base_data.get("type") or "normal",
                "background": base_data.get("background") or user_data.get("background"),
                "trigger": user_data.get("trigger") or base_data.get("trigger"),
                "rule": user_data.get("rule") or base_data.get("rule"),
                "npcs": user_data.get("npcs") or base_data.get("npcs") or [],
                "enemies": user_data.get("enemies") or base_data.get("enemies") or [],
                "transitions": []  # 나중에 처리
            }
            final_scenes.append(merged_scene)

        # B. 엔딩(Ending)인 경우
        elif node_type == "ending":
            new_id = f"Ending-{ending_counter}"
            ending_counter += 1
            id_map[curr_id] = new_id

            base_data = generated_ending_map.get(curr_id_lower, {})
            user_data = curr_node.get("data", {})

            merged_ending = {
                "ending_id": new_id,
                "title": user_data.get("title") or base_data.get("title") or "Ending",
                "description": base_data.get("description") or user_data.get("description") or "엔딩 내용 없음",
                "background": base_data.get("background") or user_data.get("background"),
                "type": "ending"
            }
            final_endings.append(merged_ending)

        # 다음 노드 탐색
        for neighbor in adj_list.get(curr_id, []):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)

    # 4. Transitions 연결 및 프롤로그 연결 업데이트

    # 4-1. 씬들의 Transitions 업데이트
    for scene in final_scenes:
        # 원래 Old ID 찾기 (역추적은 비효율적이니 순서대로 처리했다 가정하거나, Old ID를 임시 저장했어야 함)
        # -> 간단하게: id_map을 뒤집어서 Old ID를 찾거나, 애초에 final_scenes 생성 시 Old ID를 저장해두면 됨.
        # 여기서는 raw_edges를 다시 순회하며 처리

        # 현재 씬의 New ID -> Old ID 찾기
        old_id = next((k for k, v in id_map.items() if v == scene["scene_id"]), None)
        if not old_id: continue

        # 이 노드에서 나가는 엣지 찾기
        out_edges = [e for e in raw_edges if e.get("source") == old_id]

        transitions = []
        for edge in out_edges:
            tgt_old = edge.get("target")
            if tgt_old in id_map:
                tgt_new = id_map[tgt_old]

                # 유저가 작성한 트리거가 있다면 사용, 없으면 AI 생성 데이터에서 찾아보기(복잡하므로 생략하거나 기본값)
                # 우선순위: 유저 그래프의 trigger -> 노드 데이터 -> 기본값
                # 근데 엣지에는 데이터가 별로 없으므로 타겟 노드의 트리거를 주로 씀.
                # 여기서는 씬 생성 시점에 AI가 만든 transition 텍스트를 가져오기 어려우므로(매핑이 꼬임),
                # 타겟 씬의 'trigger' 필드를 가져와서 사용함.

                trigger_text = "이동"
                # 타겟이 씬이면 그 씬의 진입 트리거를 사용
                tgt_scene = next((s for s in final_scenes if s["scene_id"] == tgt_new), None)
                if tgt_scene and tgt_scene.get("trigger"):
                    trigger_text = tgt_scene["trigger"]

                # 타겟이 엔딩이면?
                tgt_ending = next((e for e in final_endings if e["ending_id"] == tgt_new), None)
                if tgt_ending:  # 엔딩은 트리거가 보통 없으므로 적절히 처리하거나 이전 씬에서 정해야 함
                    trigger_text = "엔딩으로"

                transitions.append({
                    "trigger": trigger_text,
                    "target_scene_id": tgt_new
                })

        scene["transitions"] = transitions

    # 4-2. 프롤로그 연결 업데이트
    final_prologue_connects = []
    for neighbor in start_neighbors:
        if neighbor in id_map:
            final_prologue_connects.append(id_map[neighbor])

    # 첫 씬 ID 설정
    start_scene_id = final_prologue_connects[0] if final_prologue_connects else None

    # 5. NPC 데이터 최종 병합
    final_npcs = state["characters"]
    user_npcs = {n.get("name"): n for n in state["graph_data"].get("npcs", []) if isinstance(n, dict)}
    for npc in final_npcs:
        u_npc = user_npcs.get(npc["name"])
        if u_npc: npc.update(u_npc)
    existing_names = {n["name"] for n in final_npcs}
    for u_npc in state["graph_data"].get("npcs", []):
        if isinstance(u_npc, dict) and u_npc.get("name") not in existing_names:
            final_npcs.append(u_npc)

    # 6. 최종 데이터 구성
    final_data = {
        "title": scenario_data.get("title", "Untitled"),
        "desc": scenario_data.get("summary", ""),
        "prologue": final_prologue,
        "world_settings": final_hidden,
        "player_status": final_hidden,
        "prologue_connects_to": final_prologue_connects,
        "scenario": scenario_data,
        "worlds": state["worlds"],
        "npcs": final_npcs,
        "scenes": final_scenes,
        "endings": final_endings,  # 이제 엔딩은 여기에만 들어감
        "start_scene_id": start_scene_id,
        "initial_state": initial_player_state,
        "raw_graph": state["graph_data"]
    }

    return {"final_data": final_data}


def audit_content_node(state: BuilderState):
    report_progress("building", "5/5", "최종 콘텐츠 검수 중...", 95, phase="auditing")
    final_data = state.get("final_data", {})
    try:
        if hasattr(AiAuditService, 'audit_scenario'):
            audit_result = AiAuditService.audit_scenario(final_data)
        elif hasattr(AiAuditService, 'analyze'):
            audit_result = AiAuditService.analyze(final_data)
        else:
            audit_result = {"valid": True, "info": "Audit method not found"}
        final_data["audit_report"] = audit_result
    except Exception as e:
        logger.error(f"Audit failed: {e}")
        final_data["audit_report"] = {"valid": True, "warnings": [f"검수 중 오류 발생: {e}"]}
    return {"final_data": final_data}


def build_builder_graph():
    workflow = StateGraph(BuilderState)
    workflow.add_node("validate", validate_structure)
    workflow.add_node("parse", parse_graph_to_blueprint)
    workflow.add_node("parallel_gen", parallel_generation_node)
    workflow.add_node("finalize", finalize_build)
    workflow.add_node("audit", audit_content_node)

    workflow.set_entry_point("validate")
    workflow.add_edge("validate", "parse")
    workflow.add_edge("parse", "parallel_gen")
    workflow.add_edge("parallel_gen", "finalize")
    workflow.add_edge("finalize", "audit")
    workflow.add_edge("audit", END)

    return workflow.compile()


def generate_scenario_from_graph(api_key, user_data, model_name=None):
    app = build_builder_graph()
    if not model_name and isinstance(user_data, dict) and 'model' in user_data:
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
        ("system", PROMPTS.get("generate_single_npc", "Create a TRPG NPC.")),
        ("user", f"제목:{scenario_title}\n요청:{user_request}")
    ]).partial(format_instructions=parser.get_format_instructions())
    return safe_invoke_json(prompt | llm | parser, {}, retries=1)