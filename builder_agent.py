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
        # phase가 없으면 step을 기반으로 추론하거나 기본값 설정
        payload = {
            "status": status,
            "step": step,
            "detail": detail,
            "progress": progress,
            "current_phase": phase or "initializing"  # 프론트엔드가 기대하는 키 추가
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
    player_prologue: str = Field(description="[공개용 프롤로그] 게임 시작 시 화면에 출력되어 플레이어가 읽게 될 도입부 텍스트. 분위기 있고 흥미롭게 작성.")
    gm_notes: str = Field(
        description="[시스템 내부 설정] 플레이어에게는 비밀로 하고 시스템(GM)이 관리할 전체 설정, 진실, 트릭, 히든 스탯 등. 이 내용은 Player Status로도 활용됨.")


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
    # phase 명시: 'parsing'
    report_progress("building", "1/5", "구조 분석 중...", 10, phase="parsing")
    data = state["graph_data"]
    nodes = data.get("nodes", [])
    edges = data.get("edges", [])
    raw_npcs = data.get("npcs", [])

    blueprint = "### 시나리오 구조 명세서 ###\n\n"

    start_node = next((n for n in nodes if n["type"] == "start"), None)
    if start_node:
        title = start_node['data'].get('label', '')

        # 사용자가 UI에서 입력한 값 확인
        prologue = start_node['data'].get('prologue', '')
        gm_notes = start_node['data'].get('gm_notes', '')

        # 없다면 기존 description 필드에서 파싱 시도 (호환성)
        desc = start_node['data'].get('description', '')
        if not prologue and not gm_notes and desc:
            prologue = desc

        blueprint += f"[설정]\n제목: {title}\n"
        if prologue:
            blueprint += f"프롤로그(공개): {prologue}\n"
        if gm_notes:
            blueprint += f"시스템 설정(비공개): {gm_notes}\n"
        blueprint += "\n"

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
    # phase 명시: 'worldbuilding' (설정 기획 단계)
    report_progress("building", "2/5", "개요 및 설정 기획 중...", 30, phase="worldbuilding")
    llm = LLMFactory.get_llm(state.get("model_name"))
    parser = JsonOutputParser(pydantic_object=ScenarioSummary)

    prompt = ChatPromptTemplate.from_messages([
        ("system",
         "당신은 TRPG 시나리오 작가입니다. JSON 형식으로 응답하세요.\n"
         "설계도(Blueprint)를 바탕으로 시나리오의 전체적인 개요를 작성해주세요.\n"
         "설계도에 이미 '프롤로그'나 '시스템 설정'이 작성되어 있다면, 그 내용을 최대한 유지하면서 문장을 다듬어주세요.\n"
         "다음 두 가지를 반드시 구분해서 작성해야 합니다:\n"
         "1. 'player_prologue': 게임 시작 시 플레이어 화면에 출력될 공개 텍스트 (분위기 조성용)\n"
         "2. 'gm_notes': 플레이어에게는 숨겨진 전체 설정, 세계관의 진실, 시스템 내부 로직.\n"
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
        return {"scenario": {
            "title": "Untitled",
            "summary": "",
            "player_prologue": "",
            "gm_notes": ""
        }}


def generate_full_content(state: BuilderState):
    """
    [수정됨] 세계관/NPC 생성 -> 씬 생성 (순차적 처리로 변경)
    세계관이 씬 묘사에 반영되도록 수정함.
    """
    # phase 명시: 'worldbuilding' -> 'scene_generation'
    report_progress("building", "3/5", "세계관 및 NPC 생성 중...", 50, phase="worldbuilding")
    llm = LLMFactory.get_llm(state.get("model_name"))
    blueprint = state.get("blueprint", "")

    # 1. NPC 및 World 우선 생성
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
                ("system",
                 "설계도에 명시된 세계관/배경 설정을 정확히 반영하여 배경 장소 3곳을 묘사하세요.\n"
                 "{format_instructions}"),
                ("user", "{blueprint}")
            ]).partial(format_instructions=world_parser.get_format_instructions())
            | llm | world_parser
    )

    # 병렬 실행 (NPC & World)
    setup_chain = RunnableParallel(npcs=npc_chain, worlds=world_chain)

    try:
        setup_res = setup_chain.invoke({"blueprint": blueprint})
    except Exception as e:
        logger.error(f"Setup Gen Error: {e}")
        setup_res = {"npcs": {"npcs": []}, "worlds": {"worlds": []}}

    npcs = setup_res['npcs'].get('npcs', [])
    worlds = setup_res['worlds'].get('worlds', [])

    # 생성된 세계관 정보를 텍스트로 변환하여 씬 생성 프롬프트에 주입
    report_progress("building", "3.5/5", "장면 및 사건 구성 중...", 65, phase="scene_generation")

    world_context = "\n".join([f"- 배경 '{w.get('name')}': {w.get('description')}" for w in worlds])
    npc_context = "\n".join([f"- NPC '{n.get('name')}': {n.get('role')}" for n in npcs])

    # 2. Scene 생성 (세계관 정보 참조)
    scene_parser = JsonOutputParser(pydantic_object=SceneData)
    scene_prompt = ChatPromptTemplate.from_messages([
        ("system",
         "설계도와 생성된 세계관/NPC 정보를 바탕으로 씬 데이터를 생성하세요.\n"
         "ID는 절대 변경하지 마세요.\n"
         "각 장면의 'description' 작성 시, 생성된 '배경 장소(World)'의 묘사를 적극적으로 인용하여 현장감을 살리세요.\n"
         "연결(Transition) 생성 시 구체적인 행동(문을 연다 등)을 만드세요.\n"
         "{format_instructions}"),
        ("user",
         f"설계도:\n{blueprint}\n\n"
         f"참고할 세계관 설정:\n{world_context}\n\n"
         f"등장 NPC:\n{npc_context}")
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


def polish_content(state: BuilderState):
    logger.info("Validator(polish_content) checking...")
    # phase 명시: 'validation'
    report_progress("building", "4/5", "품질 검수 및 보정 중...", 80, phase="validation")
    llm = LLMFactory.get_llm(state.get("model_name"))

    scenes = state["scenes"]
    endings = state["endings"]
    scenario_title = state["scenario"].get("title", "")

    items_to_fix = []

    # 엔딩 검사
    for end in endings:
        if not end.get("description") or len(end.get("description")) < 10:
            items_to_fix.append(f"[엔딩 보강] ID '{end.get('ending_id')}': 설명이 너무 짧거나 비어있음.")

    # 씬 검사
    for scene in scenes:
        if not scene.get("description"):
            items_to_fix.append(f"[장면 보강] ID '{scene.get('scene_id')}': 설명(description)이 비어있음. 세계관을 활용해 채워넣을 것.")

    # 트리거 검사
    for scene in scenes:
        for trans in scene.get("transitions", []):
            trig = trans.get("trigger", "")
            if "이동" in trig or "Move" in trig or len(trig) < 2:
                items_to_fix.append(
                    f"[트리거 수정] Scene '{scene.get('scene_id')}' -> '{trans.get('target_scene_id')}': '{trig}'를 구체적 행동으로 변경.")

    if not items_to_fix:
        return state

    # LLM Patch
    prompt = ChatPromptTemplate.from_messages([
        ("system",
         "지적된 문제점들을 해결하여 '수정된 데이터만' JSON으로 출력하세요.\n{format_instructions}"),
        ("user",
         f"제목: {scenario_title}\n수정 요청:\n" + "\n".join(items_to_fix))
    ])

    parser = JsonOutputParser(pydantic_object=PatchResult)
    chain = prompt | llm | parser

    try:
        patch_data = chain.invoke({"format_instructions": parser.get_format_instructions()})

        # Apply Patch
        updates_endings = {p['ending_id']: p['description'] for p in patch_data.get('endings', [])}
        if updates_endings:
            for end in endings:
                if end['ending_id'] in updates_endings:
                    end['description'] = updates_endings[end['ending_id']]

        updates_transitions = {(p['scene_id'], p['target_scene_id']): p['new_trigger'] for p in
                               patch_data.get('transitions', [])}
        if updates_transitions:
            for scene in scenes:
                for trans in scene.get('transitions', []):
                    key = (scene['scene_id'], trans['target_scene_id'])
                    if key in updates_transitions:
                        trans['trigger'] = updates_transitions[key]

        return {"scenes": scenes, "endings": endings}

    except Exception as e:
        logger.error(f"Polish Error: {e}")
        return state


class InitialStateExtractor(BaseModel):
    hp: Optional[int] = Field(None, description="체력 (언급 없으면 null)")
    mp: Optional[int] = Field(None, description="마력 (언급 없으면 null)")
    sanity: Optional[int] = Field(None, description="정신력/이성 (언급 없으면 null)")
    gold: Optional[int] = Field(None, description="소지금/골드 (언급 없으면 null)")
    inventory: Optional[List[str]] = Field(None, description="시작 아이템 목록 (언급 없으면 null)")


def finalize_build(state: BuilderState):
    # phase 명시: 'finalizing'
    report_progress("building", "5/5", "최종 마무리 중...", 100, phase="finalizing")
    data = state["graph_data"]
    start_id = None

    # 시작점 찾기
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

    # 프롤로그/설정 매핑
    scenario_data = state.get("scenario", {})
    generated_prologue = scenario_data.get("player_prologue", "")
    generated_hidden = scenario_data.get("gm_notes", "")

    # 사용자 입력값(raw) fallback
    raw_prologue = start_node.get("data", {}).get("prologue", "") if start_node else ""
    raw_gm_notes = start_node.get("data", {}).get("gm_notes", "") if start_node else ""

    final_prologue = generated_prologue if generated_prologue else raw_prologue
    final_hidden = generated_hidden if generated_hidden else raw_gm_notes

    # [수정됨] 생성된 씬 내용을 raw_graph 노드에 역으로 업데이트 (프론트엔드 동기화 + 대소문자 호환)
    raw_nodes = state["graph_data"].get("nodes", [])

    # ID 매칭을 위해 소문자 키 맵 생성
    scene_map = {s["scene_id"].lower(): s for s in state["scenes"]}
    ending_map = {e["ending_id"].lower(): e for e in state["endings"]}

    for node in raw_nodes:
        nid = node["id"].lower()  # raw_graph ID도 소문자로 변환하여 비교

        if nid in scene_map:
            tgt = scene_map[nid]
            node["data"]["title"] = tgt["name"]
            node["data"]["description"] = tgt["description"]
            node["data"]["npcs"] = tgt["npcs"]

        elif nid in ending_map:
            tgt = ending_map[nid]
            node["data"]["title"] = tgt["title"]
            node["data"]["description"] = tgt["description"]

    # [수정: 초기 스탯 파싱 로직 강화]
    initial_player_state = {
        "hp": 100,
        "inventory": []
    }

    # 제목/장르 기반 1차 설정 (기존 로직 유지)
    scenario_title = state["scenario"].get("title", "Untitled")
    if any(k in scenario_title.lower() for k in ["던전", "모험", "전투", "raid"]):
        initial_player_state.update({"mp": 50, "attack": 10, "gold": 0})
    elif any(k in scenario_title.lower() for k in ["공포", "호러", "크툴루", "mystery"]):
        initial_player_state.update({"sanity": 100, "flashlight": 1})

    # 2. [신규 추가] LLM을 이용한 GM 노트 파싱 (Override)
    try:
        extract_llm = LLMFactory.get_llm(state.get("model_name"), temperature=0.1)  # 정확도를 위해 낮은 온도 사용
        parser = JsonOutputParser(pydantic_object=InitialStateExtractor)

        extract_prompt = ChatPromptTemplate.from_messages([
            ("system",
             "당신은 게임 데이터 분석가입니다. 주어진 '시스템 설정(GM Note)' 텍스트를 분석하여 "
             "플레이어의 시작 스탯(HP, MP, Sanity, Gold, Inventory)이 명시되어 있다면 추출하세요.\n"
             "명시되지 않은 항목은 null로 반환하세요.\n"
             "{format_instructions}"),
            ("user", f"분석할 텍스트:\n{final_hidden}")
        ]).partial(format_instructions=parser.get_format_instructions())

        # LLM 호출
        extracted_stats = (extract_prompt | extract_llm | parser).invoke({})

        # 추출된 값이 있는 경우에만 기존 state 업데이트 (None 제외)
        if extracted_stats:
            for key, value in extracted_stats.items():
                if value is not None and value != []:
                    # 기존에 값이 있더라도 GM 노트의 설정이 우선하므로 덮어씌움
                    initial_player_state[key] = value
                    logger.info(f"✅ Extracted Stat Applied: {key} = {value}")

    except Exception as e:
        logger.warning(f"Stats Extraction Failed: {e} (Using default templates)")
        # 실패해도 치명적이지 않음 -> 기본 템플릿 값 사용

    final_data = {
        "title": scenario_title,
        "desc": scenario_data.get("summary", ""),
        "prologue": final_prologue,
        "world_settings": final_hidden,  # GM Note (Engine에서 사용)
        "player_status": final_hidden,  # Legacy
        "prologue_connects_to": prologue_connects,
        "scenario": scenario_data,
        "worlds": state["worlds"],  # Global World Info (Engine에서 사용)
        "npcs": state["characters"],
        "scenes": state["scenes"],
        "endings": state["endings"],
        "start_scene_id": start_id,
        "initial_state": initial_player_state,
        "raw_graph": state["graph_data"]  # 업데이트된 그래프 데이터 저장
    }

    final_data = renumber_scenes_bfs(final_data)

    return {"final_data": final_data}


def build_builder_graph():
    workflow = StateGraph(BuilderState)
    workflow.add_node("parse", parse_graph_to_blueprint)
    workflow.add_node("refine", refine_scenario_info)
    workflow.add_node("generate", generate_full_content)
    workflow.add_node("polish", polish_content)
    workflow.add_node("finalize", finalize_build)

    workflow.set_entry_point("parse")
    workflow.add_edge("parse", "refine")
    workflow.add_edge("refine", "generate")
    workflow.add_edge("generate", "polish")
    workflow.add_edge("polish", "finalize")
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
        ("system", "캐릭터 생성기\n{format_instructions}"),
        ("user", f"제목:{scenario_title}\n요청:{user_request}")
    ]).partial(format_instructions=parser.get_format_instructions())
    return (prompt | llm | parser).invoke({})