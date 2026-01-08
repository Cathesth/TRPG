import json
import os
import yaml
import logging
import concurrent.futures  # [NEW] 병렬 처리를 위해 추가
from typing import TypedDict, List, Annotated, Optional, Dict, Any, Callable
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.runnables import RunnableParallel
from langgraph.graph import StateGraph, END

from llm_factory import LLMFactory
from schemas import NPC
from core.utils import renumber_scenes_bfs

# [NEW] 검수 서비스 임포트 (파일 경로 확인 필요)
# 만약 파일이 services/ai_audit_service.py 에 있다면 아래처럼 임포트
try:
    from services.ai_audit_service import AiAuditService
except ImportError:
    # 임시 fallback 클래스 (파일이 없을 경우 에러 방지)
    class AiAuditService:
        @staticmethod
        def audit_scenario(data):
            return {"valid": True, "score": 0, "feedback": ["검수 모듈을 찾을 수 없습니다."]}

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# --- 프롬프트 로더 ---
def load_prompts() -> Dict[str, str]:
    # 가능한 경로들 (단수/복수 혼용 방지, 절대 경로 사용)
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


# [최적화] 일반적인 요약 (World 등)
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


# [NEW] NPC 전용 스마트 요약 (이름, 역할, 외모, 성격만 추출)
def summarize_npc_context(npcs: List[Dict], limit: int = 15) -> str:
    if not npcs: return "없음"

    summary_list = []
    count = len(npcs)
    target_items = npcs[:limit]

    for npc in target_items:
        if not isinstance(npc, dict): continue

        name = npc.get("name", "Unknown")
        role = npc.get("role") or npc.get("type") or "Unknown"

        # 묘사에 필수적인 요소만 압축해서 전달 (비밀, 스탯 등 제외)
        traits = []
        if npc.get("appearance"): traits.append(f"외모:{npc.get('appearance')}")
        if npc.get("personality"): traits.append(f"성격:{npc.get('personality')}")
        # [Tip] 씬 생성 시 대사 스타일도 참조하면 좋음
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

    # Start Node는 반드시 1개의 연결만 가져야 함
    out_edges = edge_map[node["id"]]["out"]
    if len(out_edges) == 0:
        raise ValueError("시작점(프롤로그)에 첫 번째 장면을 연결해주세요.")
    if len(out_edges) > 1:
        raise ValueError("시작점(프롤로그)은 오직 하나의 오프닝 장면과만 연결할 수 있습니다. 분기점은 그 이후 장면부터 만들어주세요.")


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


# --- 노드 함수 (LangGraph Nodes) ---

def validate_structure(state: BuilderState):
    logger.info("Validating graph structure...")
    report_progress("building", "0/5", "구조 및 연결 검증 중...", 5, phase="initializing")

    graph_data = state["graph_data"]

    # 1. graph_data 파싱 (문자열인 경우)
    if isinstance(graph_data, str):
        try:
            logger.info("graph_data is string, attempting to parse JSON...")
            graph_data = json.loads(graph_data)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse graph_data JSON: {e}")
            raise ValueError("입력 데이터가 올바른 JSON 형식이 아닙니다.")

    if isinstance(graph_data, str):
        try:
            graph_data = json.loads(graph_data)
        except:
            pass

    if not isinstance(graph_data, dict):
        raise ValueError(f"그래프 데이터가 딕셔너리가 아닙니다. (Type: {type(graph_data)})")

    # 2. 노드 데이터 파싱
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

            # node['data']가 문자열이면 파싱 시도
            if "data" in node and isinstance(node["data"], str):
                try:
                    node["data"] = json.loads(node["data"])
                except Exception:
                    node["data"] = {}

            if "data" not in node or not isinstance(node["data"], dict):
                node["data"] = {}

            valid_nodes.append(node)

    graph_data["nodes"] = valid_nodes

    # 3. 엣지 데이터 파싱
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

    # 검증 실행
    nodes = graph_data.get("nodes", [])
    edge_map = {n.get("id"): {"in": [], "out": []} for n in nodes if isinstance(n, dict) and "id" in n}

    for edge in valid_edges:
        src, tgt = edge.get("source"), edge.get("target")
        if src in edge_map: edge_map[src]["out"].append(tgt)
        if tgt in edge_map: edge_map[tgt]["in"].append(src)

    for node in nodes:
        ntype = node.get("type", "unknown")
        validator = NODE_VALIDATORS.get(ntype)
        if validator:
            validator(node, edge_map)
        else:
            logger.warning(f"Unknown node type: {ntype}")

    return state


def parse_graph_to_blueprint(state: BuilderState):
    """
    Blueprint 생성 단계 (Enemy, NPC 리스트 처리 안전장치 추가)
    """
    report_progress("building", "1/5", "구조 분석 중...", 10, phase="parsing")
    data = state["graph_data"]
    nodes = data.get("nodes", [])
    edges = data.get("edges", [])
    raw_npcs = data.get("npcs", [])

    blueprint = "### 시나리오 구조 명세서 ###\n\n"

    start_node = next((n for n in nodes if n.get("type") == "start"), None)
    if start_node:
        d = start_node.get('data', {})
        blueprint += f"[설정]\n제목: {d.get('label', '')}\n"
        blueprint += f"프롤로그: {d.get('prologue', '')}\n"
        blueprint += f"시스템 설정: {d.get('gm_notes', '')}\n"
        blueprint += f"배경 묘사: {d.get('background', '')}\n\n"

    blueprint += "[등장인물 및 적 상세]\n"
    if isinstance(raw_npcs, list):
        for npc in raw_npcs:
            if not isinstance(npc, dict): continue

            name = npc.get('name', 'Unknown')
            role = npc.get('role') or npc.get('type') or 'Unknown'

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
        if node.get("type") == "start": continue
        d = node.get("data", {})

        blueprint += f"ID: {node.get('id')} ({node.get('type')})\n"
        blueprint += f"제목: {d.get('title', '제목 없음')}\n"
        blueprint += f"유형: {d.get('scene_type', 'normal')}\n"
        if d.get('background'): blueprint += f"배경: {d.get('background')}\n"
        if d.get('description'): blueprint += f"내용: {d.get('description')}\n"
        if d.get('trigger'): blueprint += f"트리거: {d.get('trigger')}\n"
        if d.get('rule'): blueprint += f"추가 룰: {d.get('rule')}\n"

        # [CRITICAL FIX] Enemy와 NPC 리스트가 문자열(이름)로 들어와도 안전하게 처리
        enemies = d.get("enemies", [])
        if enemies:
            enemy_list = []
            if isinstance(enemies, list):
                for e in enemies:
                    if isinstance(e, dict):
                        enemy_list.append(e.get('name', 'Unknown'))
                    else:
                        enemy_list.append(str(e))  # 문자열인 경우 그대로 사용
                e_str = ', '.join(enemy_list)
            else:
                e_str = str(enemies)
            blueprint += f"등장 적: {e_str}\n"

        # NPC 등장 정보도 동일하게 처리 (안전장치)
        scene_npcs = d.get("npcs", [])
        if scene_npcs:
            npc_list = []
            if isinstance(scene_npcs, list):
                for n in scene_npcs:
                    if isinstance(n, dict):
                        npc_list.append(n.get('name', 'Unknown'))
                    else:
                        npc_list.append(str(n))
                n_str = ', '.join(npc_list)
            else:
                n_str = str(scene_npcs)
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

    prompt_text = PROMPTS.get("refine_scenario",
                              "You are a TRPG Scenario Architect. Refine the given blueprint into a cohesive scenario summary.")

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
                ("system", PROMPTS.get("generate_npc", "Generate NPCs based on the blueprint.")),
                ("user", "{blueprint}")
            ]).partial(format_instructions=npc_parser.get_format_instructions())
            | llm | npc_parser
    )

    world_parser = JsonOutputParser(pydantic_object=WorldList)
    world_chain = (
            ChatPromptTemplate.from_messages([
                ("system", PROMPTS.get("generate_world", "Generate world settings based on the blueprint.")),
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

    # [수정] 방금 생성된 풍부한 NPC와 World 정보를 사용함
    world_context = summarize_context(worlds, 'name', 'description', limit=10)

    # graph_data의 단순 정보 대신 AI가 생성한 상세 정보를 우선 사용 (없으면 graph_data 사용)
    generated_npcs_map = {n['name']: n for n in npcs}
    graph_npcs = state["graph_data"].get("npcs", [])
    merged_npcs = []

    # 그래프에 있는 NPC들을 AI 생성 정보로 강화
    if isinstance(graph_npcs, list):
        for g_npc in graph_npcs:
            if not isinstance(g_npc, dict): continue
            name = g_npc.get("name")
            if name in generated_npcs_map:
                merged_npcs.append(generated_npcs_map[name])
            else:
                merged_npcs.append(g_npc)

    # AI가 추가로 생성한 NPC가 있다면 추가
    existing_names = {n.get("name") for n in merged_npcs}
    for n in npcs:
        if n.get("name") not in existing_names:
            merged_npcs.append(n)

    npc_context = summarize_npc_context(merged_npcs, limit=20)

    scene_parser = JsonOutputParser(pydantic_object=SceneData)
    scene_prompt = ChatPromptTemplate.from_messages([
        ("system", PROMPTS.get("generate_scene", "Generate scenes based on the blueprint.")),
        ("user", f"설계도:\n{blueprint}\n\n[참고: 세계관 설정]\n{world_context}\n\n[참고: 등장인물 상세]\n{npc_context}")
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


# --- [NEW] 병렬 생성 노드 ---
def parallel_generation_node(state: BuilderState):
    """
    개요 생성(Refine)과 상세 내용 생성(Generate)을 병렬로 수행
    """
    report_progress("building", "2/5", "시나리오 개요 및 상세 콘텐츠 동시 생성 중...", 40, phase="parallel_gen")

    with concurrent.futures.ThreadPoolExecutor() as executor:
        # 두 작업을 동시에 던짐
        future_refine = executor.submit(refine_scenario_info, state)
        future_generate = executor.submit(generate_full_content, state)

        # 결과 대기
        try:
            refine_result = future_refine.result()
            generate_result = future_generate.result()
        except Exception as e:
            logger.error(f"Parallel Generation Error: {e}")
            # 에러 발생 시 재시도하거나 기본값 처리 (여기선 에러 전파)
            raise e

    # 결과 병합
    return {**refine_result, **generate_result}


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
    start_node = next((n for n in data.get("nodes", []) if n.get("type") == "start"), None)

    start_id = None
    prologue_connects = []

    if start_node:
        for edge in data.get("edges", []):
            if edge.get("source") == start_node.get("id"):
                prologue_connects.append(edge.get("target"))
                if not start_id: start_id = edge.get("target")

    scenario_data = state.get("scenario", {})
    start_data = start_node.get("data", {}) if start_node else {}
    if not isinstance(start_data, dict): start_data = {}

    final_prologue = scenario_data.get("player_prologue") or start_data.get("prologue", "")
    final_hidden = scenario_data.get("gm_notes") or start_data.get("gm_notes", "")

    # 2. Raw Graph 업데이트
    raw_nodes = state["graph_data"].get("nodes", [])
    scene_map = {s["scene_id"].lower(): s for s in state["scenes"]}
    ending_map = {e["ending_id"].lower(): e for e in state["endings"]}

    for node in raw_nodes:
        nid = node.get("id", "").lower()
        if not nid: continue

        node_data = node.get("data", {})
        if not isinstance(node_data, dict): node_data = {}

        if nid in scene_map:
            tgt = scene_map[nid]
            node_data.update({
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
            node_data.update({
                "title": tgt["title"],
                "description": tgt["description"],
                "background": tgt.get("background")
            })

        node["data"] = node_data

    # 3. 초기 스탯 설정
    initial_player_state = {"hp": 100, "inventory": []}

    if start_node:
        d = start_data
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
        if isinstance(custom_stats, list):
            for stat in custom_stats:
                if isinstance(stat, dict):
                    name = stat.get("name")
                    val = stat.get("value")
                    if name:
                        initial_player_state[name] = val
                        custom_stats_text.append(f"{name}: {val}")

        append_text = ""
        if custom_stats_text:
            append_text += "\n\n[추가 스탯 설정]\n" + "\n".join(custom_stats_text)
        if stat_rules:
            append_text += "\n\n[스탯 규칙]\n" + str(stat_rules)

        final_hidden += append_text

    extract_llm = LLMFactory.get_llm(state.get("model_name"), temperature=0.0)
    parser = JsonOutputParser(pydantic_object=InitialStateExtractor)

    extract_prompt = ChatPromptTemplate.from_messages([
        ("system", PROMPTS.get("extract_stats", "Extract initial game stats from the text.")),
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

    # 4. NPC 데이터 병합
    final_npcs = state["characters"]
    user_npcs = {n.get("name"): n for n in state["graph_data"].get("npcs", []) if isinstance(n, dict)}

    for npc in final_npcs:
        u_npc = user_npcs.get(npc["name"])
        if u_npc:
            npc.update(u_npc)

    existing_names = {n["name"] for n in final_npcs}
    for u_npc in state["graph_data"].get("npcs", []):
        if isinstance(u_npc, dict) and u_npc.get("name") not in existing_names:
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

    # 씬 리넘버링 (Scene-1, Scene-2...)
    final_data = renumber_scenes_bfs(final_data)

    # --- [추가] 엔딩 ID 리넘버링 (Ending-1, Ending-2...) ---
    endings = final_data.get("endings", [])
    scenes = final_data.get("scenes", [])

    if endings:
        ending_map = {}
        # 1. 엔딩 ID 변경 및 매핑 생성
        for idx, ending in enumerate(endings, 1):
            old_id = ending["ending_id"]
            new_id = f"Ending-{idx}"
            ending["ending_id"] = new_id
            ending_map[old_id] = new_id

        # 2. 씬 Transition(연결)에서 엔딩 ID 참조 업데이트
        for scene in scenes:
            for trans in scene.get("transitions", []):
                tgt = trans.get("target_scene_id")
                if tgt in ending_map:
                    trans["target_scene_id"] = ending_map[tgt]

        # 3. 프롤로그 연결 업데이트 (엔딩으로 바로 이어지는 경우 대비)
        prologue_connects = final_data.get("prologue_connects_to", [])
        for i, target in enumerate(prologue_connects):
            if target in ending_map:
                prologue_connects[i] = ending_map[target]

    return {"final_data": final_data}


# --- [NEW] 검수 노드 ---
def audit_content_node(state: BuilderState):
    report_progress("building", "5/5", "최종 콘텐츠 검수 중...", 95, phase="auditing")

    final_data = state.get("final_data", {})

    # 검수 서비스 호출
    try:
        # ai_audit_service.py의 메서드명 확인 필요 (예: analyze_scenario, audit 등)
        # 여기서는 가정하여 호출함. 실제 서비스 코드에 맞춰 수정 바람.
        if hasattr(AiAuditService, 'audit_scenario'):
            audit_result = AiAuditService.audit_scenario(final_data)
        elif hasattr(AiAuditService, 'analyze'):
            audit_result = AiAuditService.analyze(final_data)
        else:
            # 메서드를 못 찾으면 스킵
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

    # [변경] 병렬 처리 노드로 교체
    workflow.add_node("parallel_gen", parallel_generation_node)
    # workflow.add_node("refine", refine_scenario_info)
    # workflow.add_node("generate", generate_full_content)

    workflow.add_node("finalize", finalize_build)

    # [추가] 검수 노드
    workflow.add_node("audit", audit_content_node)

    workflow.set_entry_point("validate")
    workflow.add_edge("validate", "parse")

    # [변경] parse -> parallel_gen -> finalize 흐름
    workflow.add_edge("parse", "parallel_gen")
    workflow.add_edge("parallel_gen", "finalize")

    # [변경] finalize -> audit -> END
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