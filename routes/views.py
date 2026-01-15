from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
import logging

from config import get_full_version
from routes.auth import get_current_user_optional, get_current_user
from models import get_db, Scenario
from services.mermaid_service import MermaidService
from services.scenario_service import ScenarioService

logger = logging.getLogger(__name__)

views_router = APIRouter(tags=["views"])
templates = Jinja2Templates(directory="templates")


@views_router.get("/", response_class=HTMLResponse)
async def index(request: Request, user=Depends(get_current_user_optional)):
    """메인 페이지"""
    return templates.TemplateResponse("index.html", {
        "request": request,
        "version": get_full_version(),
        "user": user
    })


@views_router.get("/views/builder", response_class=HTMLResponse)
async def view_builder(request: Request, user=Depends(get_current_user)):
    """빌더 뷰 (로그인 필수)"""
    return templates.TemplateResponse("builder_view.html", {
        "request": request,
        "version": get_full_version(),
        "user": user
    })


@views_router.get("/views/player", response_class=HTMLResponse)
async def view_player(request: Request, user=Depends(get_current_user_optional)):
    """플레이어 뷰 (세션별 독립 데이터)"""
    # 전역 game_state 대신 빈 딕셔너리 사용 (클라이언트가 세션 데이터 로드)
    p_vars = {}
    return templates.TemplateResponse("player_view.html", {
        "request": request,
        "vars": p_vars,
        "version": get_full_version(),
        "user": user
    })


@views_router.get("/views/scenes", response_class=HTMLResponse)
async def view_scenes(request: Request, user=Depends(get_current_user_optional)):
    """씬 맵 뷰 (세션 독립 - 클라이언트가 세션 데이터를 전달해야 함)"""
    # 전역 game_state 제거 - 클라이언트가 시나리오 ID를 URL 파라미터로 전달해야 함
    return templates.TemplateResponse("scenes_view.html", {
        "request": request,
        "title": "Scene Map",
        "scenario": {"endings": [], "prologue_text": ""},
        "scenes": [],
        "current_scene_id": None,
        "mermaid_code": "graph TD\n    A[시나리오를 먼저 로드하세요]",
        "scene_display_ids": {},
        "ending_display_ids": {},
        "edit_mode": False,
        "scenario_id": None,
        "version": get_full_version(),
        "user": user
    })


@views_router.get("/views/debug_scenes", response_class=HTMLResponse)
async def view_debug_scenes(
    request: Request,
    scenario_id: str = Query(None, description="시나리오 ID"),
    user=Depends(get_current_user_optional),
    db: Session = Depends(get_db)
):
    """
    디버그 모드 전체 씬 보기 (플레이어 모드에서 접근)
    ✅ [FIX 3] scenario_id를 쿼리 파라미터로 받거나 sessionStorage에서 복원
    """

    # ✅ scenario_id가 없으면 기본 페이지 반환 (프론트엔드에서 sessionStorage 복원 시도)
    if not scenario_id:
        return templates.TemplateResponse("debug_scenes_view.html", {
            "request": request,
            "title": "Debug Scene Map",
            "scenario": {"endings": [], "prologue_text": ""},
            "scenes": [],
            "current_scene_id": None,
            "mermaid_code": "graph TD\n    A[시나리오 로드 중...]",
            "scene_display_ids": {},
            "ending_display_ids": {},
            "scene_names": {},
            "ending_names": {},
            "incoming_conditions": {},
            "ending_incoming_conditions": {},
            "version": get_full_version(),
            "user": user,
            "scenario_id": None
        })

    # ✅ scenario_id가 있으면 DB에서 시나리오 로드
    try:
        from services.scenario_service import ScenarioService
        from services.mermaid_service import MermaidService

        # 시나리오 조회
        result, error = ScenarioService.get_scenario_for_view(int(scenario_id), user.id if user else None, db)

        if error or not result:
            return templates.TemplateResponse("debug_scenes_view.html", {
                "request": request,
                "title": "시나리오를 찾을 수 없음",
                "scenario": {"endings": [], "prologue_text": ""},
                "scenes": [],
                "current_scene_id": None,
                "mermaid_code": "graph TD\n    A[시나리오를 찾을 수 없습니다]",
                "scene_display_ids": {},
                "ending_display_ids": {},
                "scene_names": {},
                "ending_names": {},
                "incoming_conditions": {},
                "ending_incoming_conditions": {},
                "version": get_full_version(),
                "user": user,
                "scenario_id": scenario_id
            })

        scenario_data = result

        # ✅ [FIX 2-B] Mermaid 그래프 생성 - 실패해도 나머지 데이터는 정상 렌더링
        mermaid_code = "graph TD\n    A[Mermaid 차트 생성 중...]"
        try:
            mermaid_code = MermaidService.generate_mermaid_from_scenario(scenario_data)
            logger.info(f"✅ [DEBUG SCENES] Mermaid chart generated successfully")
        except Exception as mermaid_error:
            logger.error(f"❌ [DEBUG SCENES] Mermaid generation failed: {mermaid_error}", exc_info=True)
            mermaid_code = "graph TD\n    Error[Mermaid 차트 생성 실패]\n    Error -->|시나리오 데이터는 정상| Info[아래 씬 목록 참조]"

        # 현재 진행 중인 씬 정보 (옵션)
        current_scene_id = None

        # Scene ID 매핑
        scene_display_ids = {s.get('scene_id'): s.get('scene_id') for s in scenario_data.get('scenes', [])}
        ending_display_ids = {e.get('ending_id'): e.get('ending_id') for e in scenario_data.get('endings', [])}

        # Scene/Ending 이름 매핑
        scene_names = {s.get('scene_id'): s.get('title', s.get('name', s.get('scene_id'))) for s in scenario_data.get('scenes', [])}
        ending_names = {e.get('ending_id'): e.get('title', e.get('ending_id')) for e in scenario_data.get('endings', [])}

        # Incoming conditions 계산
        incoming_conditions = {}
        for scene in scenario_data.get('scenes', []):
            for trans in scene.get('transitions', []):
                target_id = trans.get('target_scene_id')
                if target_id:
                    if target_id not in incoming_conditions:
                        incoming_conditions[target_id] = []
                    incoming_conditions[target_id].append({
                        'from_title': scene.get('title', scene.get('name', scene.get('scene_id'))),
                        'condition': trans.get('trigger', trans.get('condition', '자유 행동'))
                    })

        ending_incoming_conditions = {}
        for scene in scenario_data.get('scenes', []):
            for trans in scene.get('transitions', []):
                target_id = trans.get('target_scene_id')
                if target_id and target_id in ending_names:
                    if target_id not in ending_incoming_conditions:
                        ending_incoming_conditions[target_id] = []
                    ending_incoming_conditions[target_id].append({
                        'from_title': scene.get('title', scene.get('name', scene.get('scene_id'))),
                        'condition': trans.get('trigger', trans.get('condition', '자유 행동'))
                    })

        return templates.TemplateResponse("debug_scenes_view.html", {
            "request": request,
            "title": scenario_data.get('title', 'Unknown Scenario'),
            "scenario": scenario_data,
            "scenes": scenario_data.get('scenes', []),
            "current_scene_id": current_scene_id,
            "mermaid_code": mermaid_code,
            "scene_display_ids": scene_display_ids,
            "ending_display_ids": ending_display_ids,
            "scene_names": scene_names,
            "ending_names": ending_names,
            "incoming_conditions": incoming_conditions,
            "ending_incoming_conditions": ending_incoming_conditions,
            "version": get_full_version(),
            "user": user,
            "scenario_id": scenario_id
        })

    except Exception as e:
        logger.error(f"❌ Failed to load debug scenes: {e}", exc_info=True)
        return templates.TemplateResponse("debug_scenes_view.html", {
            "request": request,
            "title": "오류 발생",
            "scenario": {"endings": [], "prologue_text": ""},
            "scenes": [],
            "current_scene_id": None,
            "mermaid_code": f"graph TD\n    A[오류: {str(e)}]",
            "scene_display_ids": {},
            "ending_display_ids": {},
            "scene_names": {},
            "ending_names": {},
            "incoming_conditions": {},
            "ending_incoming_conditions": {},
            "version": get_full_version(),
            "user": user,
            "scenario_id": scenario_id
        })


@views_router.get("/views/scenes/edit/{scenario_id}", response_class=HTMLResponse)
async def view_scenes_edit(request: Request, scenario_id: str, user=Depends(get_current_user)):
    """
    기존 씬 맵 편집 라우트를 시나리오 빌더(builder_view.html)로 연결
    """
    # 1. 시나리오 권한 및 존재 여부 확인
    result, error = ScenarioService.get_scenario_for_edit(scenario_id, user.id)
    if error:
        return templates.TemplateResponse("index.html", {
            "request": request,
            "error": "접근 권한이 없거나 존재하지 않는 시나리오입니다.",
            "version": get_full_version(),
            "user": user
        })

    # 2. builder_view.html 반환 (이게 실행되면 함수 종료)
    return templates.TemplateResponse("builder_view.html", {
        "request": request,
        "version": get_full_version(),
        "user": user,
        "scenario_id": scenario_id
    })


@views_router.get("/builder/npc-generator", response_class=HTMLResponse)
async def view_npc_generator(request: Request):
    """NPC 생성기 iframe 뷰"""
    return templates.TemplateResponse("npc_generator.html", {"request": request})
