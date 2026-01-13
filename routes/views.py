from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import os

from core.state import game_state
from services.mermaid_service import MermaidService
from services.scenario_service import ScenarioService
from services.draft_service import DraftService
from config import get_full_version
from routes.auth import get_current_user_optional, get_current_user

views_router = APIRouter(tags=["views"])
templates = Jinja2Templates(directory="templates")

# 편집 모드 전용 템플릿 (백업 디렉토리 사용)
backup_templates = Jinja2Templates(directory="backup/TRPG-2026.0109.0-dev")


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
    """플레이어 뷰"""
    p_vars = {}
    if game_state.state:
        p_vars = game_state.state.get('player_vars', {})
    return templates.TemplateResponse("player_view.html", {
        "request": request,
        "vars": p_vars,
        "version": get_full_version(),
        "user": user
    })


@views_router.get("/views/scenes", response_class=HTMLResponse)
async def view_scenes(request: Request, user=Depends(get_current_user_optional)):
    """씬 맵 뷰"""
    if not game_state.state:
        return templates.TemplateResponse("scenes_view.html", {
            "request": request,
            "title": "시나리오 없음",
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

    scenario = game_state.state['scenario']
    title = scenario.get('title', 'Untitled')
    current_scene_id = game_state.state.get('current_scene_id', None)

    # Mermaid 서비스로 차트 생성
    chart_data = MermaidService.generate_chart(scenario, current_scene_id)

    return templates.TemplateResponse("scenes_view.html", {
        "request": request,
        "title": title,
        "scenario": scenario,
        "scenes": chart_data['filtered_scenes'],
        "incoming_conditions": chart_data['incoming_conditions'],
        "ending_incoming_conditions": chart_data['ending_incoming_conditions'],
        "ending_names": chart_data['ending_names'],
        "scene_names": chart_data['scene_names'],
        "scene_display_ids": chart_data['scene_display_ids'],
        "ending_display_ids": chart_data['ending_display_ids'],
        "current_scene_id": current_scene_id,
        "mermaid_code": chart_data['mermaid_code'],
        "edit_mode": False,
        "scenario_id": None,
        "version": get_full_version(),
        "user": user
    })


@views_router.get("/views/scenes/edit/{scenario_id}", response_class=HTMLResponse)
async def view_scenes_edit(request: Request, scenario_id: int, user=Depends(get_current_user)):
    """
    씬 맵 편집 모드 (백업된 scenes_view.html의 전체 기능 지원)
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

    scenario_data = result['scenario']

    # 2. Draft가 있으면 Draft 사용, 없으면 원본 사용
    draft_result, draft_error = DraftService.get_draft(scenario_id, user.id)
    if not draft_error and draft_result:
        scenario_data = draft_result['scenario']

    # 3. Mermaid 차트 생성
    title = scenario_data.get('title', 'Untitled')
    chart_data = MermaidService.generate_chart(scenario_data, None)

    # 4. 백업 디렉토리의 scenes_view.html 사용 (편집 모드 전체 기능)
    return backup_templates.TemplateResponse("scenes_view.html", {
        "request": request,
        "title": title,
        "scenario": scenario_data,
        "scenes": chart_data['filtered_scenes'],
        "incoming_conditions": chart_data['incoming_conditions'],
        "ending_incoming_conditions": chart_data['ending_incoming_conditions'],
        "ending_names": chart_data['ending_names'],
        "scene_names": chart_data['scene_names'],
        "scene_display_ids": chart_data['scene_display_ids'],
        "ending_display_ids": chart_data['ending_display_ids'],
        "current_scene_id": None,
        "mermaid_code": chart_data['mermaid_code'],
        "edit_mode": True,
        "scenario_id": scenario_id,
        "version": get_full_version(),
        "user": user
    })


@views_router.get("/builder/npc-generator", response_class=HTMLResponse)
async def view_npc_generator(request: Request):
    """NPC 생성기 iframe 뷰"""
    return templates.TemplateResponse("npc_generator.html", {"request": request})
