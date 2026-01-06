import os
from typing import TypedDict, List, Annotated, Optional
import logging
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.runnables import RunnableParallel
from langgraph.graph import StateGraph, END
from pydantic import BaseModel, Field

from llm_factory import LLMFactory

# ---------------------------------------------------------
# [수정] schemas.py에서 정의된 NPC 모델을 가져옵니다.
# ---------------------------------------------------------
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
# [추가] 빌더 에이전트 내부에서만 사용할 데이터 모델 정의
# ---------------------------------------------------------

class ScenarioSummary(BaseModel):
    """시나리오 생성 단계의 요약 정보"""
    title: str = Field(description="시나리오 제목")
    summary: str = Field(description="시나리오 전체 줄거리 요약")


class World(BaseModel):
    """세계관/장소 정보"""
    name: str = Field(description="장소 또는 설정의 이름")
    description: str = Field(description="상세 설명")


class Event(BaseModel):
    """주요 사건 정보"""
    name: str = Field(description="사건의 이름")
    description: str = Field(description="사건의 내용 및 전개")


# 리스트 파싱을 위한 래퍼 모델들
class WorldList(BaseModel):
    worlds: List[World]


class NPCList(BaseModel):
    npcs: List[NPC]  # schemas.py의 NPC 모델을 재사용


class EventList(BaseModel):
    events: List[Event]


# --- 상태 정의 (State) ---
class BuilderState(TypedDict):
    user_request: str
    scenario: dict  # ScenarioSummary dict
    worlds: List[dict]  # World dict list
    characters: List[dict]  # NPC dict list
    events: List[dict]  # Event dict list
    final_data: dict


# --- 노드(Node) 정의 ---

def parse_request(state: BuilderState):
    report_progress("building", "1/5", "요청 분석 중...", 10)
    return {"user_request": state["user_request"]}


def generate_scenario(state: BuilderState):
    report_progress("building", "2/5", "시나리오 개요 생성 중...", 30)
    llm = LLMFactory.get_llm()
    # [수정] 내부 정의한 ScenarioSummary 사용
    parser = JsonOutputParser(pydantic_object=ScenarioSummary)

    # [보강] 시스템 프롬프트에 제약조건 추가 (환각 방지 및 퀄리티 향상)
    prompt = ChatPromptTemplate.from_messages([
        ("system",
         "당신은 베테랑 TRPG 시나리오 작가입니다. "
         "주어진 요청을 분석하여 독창적이고 기승전결이 있는 시나리오 개요를 작성하세요.\n"
         "- 제목은 흥미로워야 합니다.\n"
         "- 요약은 전체 흐름을 알 수 있게 3~4문장으로 명확히 작성하세요.\n"
         "- 없는 내용을 사실인 것처럼 꾸며내거나, 요청하지 않은 형식으로 답하지 마세요.\n"
         "{format_instructions}"),
        ("user", "{request}")
    ])
    chain = prompt | llm | parser
    try:
        result = chain.invoke({
            "request": state["user_request"],
            "format_instructions": parser.get_format_instructions()
        })
        return {"scenario": result}
    except Exception as e:
        logger.error(f"Scenario gen failed: {e}")
        return {"scenario": {"title": "생성 실패", "summary": "다시 시도해주세요."}}


def generate_parallel_details(state: BuilderState):
    """세계관과 캐릭터(NPC)를 동시에 생성 (병렬 처리)"""
    report_progress("building", "3/5", "세계관 및 등장인물 생성 중...", 50)
    llm = LLMFactory.get_llm()
    scenario = state["scenario"]
    scenario_text = f"제목: {scenario.get('title')}\n개요: {scenario.get('summary')}"

    # 세계관 체인
    world_parser = JsonOutputParser(pydantic_object=WorldList)
    world_chain = (
            ChatPromptTemplate.from_messages([
                ("system",
                 "TRPG 세계관 설정 작가입니다. 시나리오의 분위기에 딱 맞는 장소 3~4곳을 구체적으로 묘사하세요.\n"
                 "- 장소 이름은 유니크하게 짓고, 겉모습과 분위기를 포함하여 설명하세요.\n"
                 "- 시나리오 배경과 모순되지 않도록 주의하세요.\n"
                 "{format_instructions}"),
                ("user", "시나리오:\n{scenario}")
            ]).partial(format_instructions=world_parser.get_format_instructions())
            | llm
            | world_parser
    )

    # 캐릭터(NPC) 체인
    # [수정] NPCList(내부 래퍼) -> NPC(schemas.py) 구조 사용
    char_parser = JsonOutputParser(pydantic_object=NPCList)
    char_chain = (
            ChatPromptTemplate.from_messages([
                ("system",
                 "TRPG 캐릭터 디자이너입니다. 시나리오의 갈등을 고조시키거나 조력할 핵심 NPC 3~4명을 생성하세요.\n"
                 "- 각 캐릭터의 '역할(Job)', '성격', '특이사항'을 구체적으로 설정하세요.\n"
                 "- 모든 필드(이름, 나이, 종족 등)를 빠짐없이 채우세요.\n"
                 "{format_instructions}"),
                ("user", "시나리오:\n{scenario}")
            ]).partial(format_instructions=char_parser.get_format_instructions())
            | llm
            | char_parser
    )

    # 병렬 실행
    parallel_chain = RunnableParallel(worlds=world_chain, characters=char_chain)

    try:
        results = parallel_chain.invoke({"scenario": scenario_text})

        # 결과 추출
        # WorldList.worlds -> List[dict]
        w_data = results['worlds'].get('worlds', []) if isinstance(results['worlds'], dict) else results['worlds']
        # NPCList.npcs -> List[dict]
        c_data = results['characters'].get('npcs', []) if isinstance(results['characters'], dict) else results[
            'characters']

        return {"worlds": w_data, "characters": c_data}
    except Exception as e:
        logger.error(f"Parallel generation failed: {e}")
        return {"worlds": [], "characters": []}


def generate_events(state: BuilderState):
    report_progress("building", "4/5", "주요 사건 구성 중...", 70)
    llm = LLMFactory.get_llm()
    parser = JsonOutputParser(pydantic_object=EventList)

    context = (
        f"시나리오: {state['scenario'].get('title')}\n"
        f"장소 목록: {[w['name'] for w in state['worlds']]}\n"
        f"주요 인물: {[c['name'] for c in state['characters']]}"
    )

    prompt = ChatPromptTemplate.from_messages([
        ("system",
         "TRPG 퀘스트 기획자입니다. 플레이어가 겪게 될 주요 사건 3~4개를 시간 순서나 논리적 흐름에 따라 구성하세요.\n"
         "- 앞서 생성된 장소와 인물을 적극적으로 활용하여 사건을 연결하세요.\n"
         "- 단순한 나열보다는 '발단-전개-위기-절정'의 드라마틱한 구조를 선호합니다.\n"
         "{format_instructions}"),
        ("user", "{context}")
    ])
    chain = prompt | llm | parser
    try:
        result = chain.invoke({
            "context": context,
            "format_instructions": parser.get_format_instructions()
        })
        e_data = result.get('events', []) if isinstance(result, dict) else result
        return {"events": e_data}
    except Exception:
        return {"events": []}


def review_content(state: BuilderState):
    report_progress("building", "5/5", "최종 마무리 중...", 90)
    # 데이터 통합
    final_data = {
        "title": state["scenario"].get("title", "Untitled"),
        "scenario": state["scenario"],
        "worlds": state["worlds"],
        "characters": state["characters"],  # 이제 NPC 구조를 따르는 딕셔너리 리스트임
        "events": state["events"]
    }
    return {"final_data": final_data}


# --- 그래프 빌드 ---
def build_builder_graph():
    workflow = StateGraph(BuilderState)
    workflow.add_node("parse_request", parse_request)
    workflow.add_node("generate_scenario", generate_scenario)
    workflow.add_node("generate_parallel_details", generate_parallel_details)
    workflow.add_node("generate_events", generate_events)
    workflow.add_node("review_content", review_content)

    workflow.set_entry_point("parse_request")
    workflow.add_edge("parse_request", "generate_scenario")
    workflow.add_edge("generate_scenario", "generate_parallel_details")
    workflow.add_edge("generate_parallel_details", "generate_events")
    workflow.add_edge("generate_events", "review_content")
    workflow.add_edge("review_content", END)

    return workflow.compile()


# --- 호환성 및 유틸리티 함수 ---

def generate_scenario_from_graph(api_key, user_data, model_name=None):
    """api.py의 init_game에서 호출하는 진입점"""
    app = build_builder_graph()

    # 입력 데이터 매핑
    user_prompt = user_data.get('prompt', '')
    genre = user_data.get('genre', '')
    if genre:
        user_prompt = f"장르: {genre}\n요청사항: {user_prompt}"

    initial_state = {
        "user_request": user_prompt,
        "scenario": {}, "worlds": [], "characters": [], "events": [], "final_data": {}
    }

    result = app.invoke(initial_state)
    return result['final_data']


def generate_single_npc(scenario_title: str, scenario_summary: str, user_request: str = ""):
    """단일 NPC 생성 함수 (팝업용)"""
    llm = LLMFactory.get_llm()
    # [수정] schemas.NPC 객체를 직접 사용하여 파싱 유도
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