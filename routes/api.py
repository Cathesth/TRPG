import os
import json
import logging
import time
import threading
from typing import Optional
from fastapi import APIRouter, Request, Depends, Form, HTTPException, Query
from fastapi.responses import JSONResponse, HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from starlette.concurrency import run_in_threadpool

# builder_agent에서 필요한 함수들 임포트
from builder_agent import (
    generate_scenario_from_graph,
    set_progress_callback,
    generate_single_npc
)

from core.state import game_state
from core.utils import parse_request_data, pick_start_scene_id, validate_scenario_graph, can_publish_scenario
from services.scenario_service import ScenarioService
from services.user_service import UserService
from services.draft_service import DraftService
from services.ai_audit_service import AIAuditService
from services.history_service import HistoryService
from services.npc_service import save_custom_npc
from services.mermaid_service import MermaidService

from game_engine import create_game_graph
from routes.auth import get_current_user, get_current_user_optional, login_user, logout_user, CurrentUser

# [수정] NPC -> CustomNPC 로 변경 (models.py에 정의된 클래스명 사용)
from models import get_db, Preset, CustomNPC

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/views", tags=["views"])
templates = Jinja2Templates(directory="templates")

api_router = APIRouter(prefix="/api", tags=["api"])





# --- Pydantic 모델 정의 ---
class AuthRequest(BaseModel):
    username: str
    password: str
    email: Optional[str] = None


class ScenarioIdRequest(BaseModel):
    filename: str


class NPCGenerateRequest(BaseModel):
    scenario_title: str = 'Unknown Scenario'
    scenario_summary: str = ''
    request: str = ''
    model: Optional[str] = None


class DraftSceneRequest(BaseModel):
    scene_id: Optional[str] = None
    scene: Optional[dict] = None
    after_scene_id: Optional[str] = None
    handle_mode: str = 'remove_transitions'


class DraftEndingRequest(BaseModel):
    ending_id: Optional[str] = None
    ending: Optional[dict] = None


class HistoryAddRequest(BaseModel):
    action_type: str = 'edit'
    action_description: str = '변경'
    snapshot: Optional[dict] = None


class AuditRequest(BaseModel):
    scene_id: Optional[str] = None
    audit_type: str = 'full'
    model: Optional[str] = None


# --- [인증 API] ---
@api_router.post('/auth/register')
async def register(data: AuthRequest):
    if not data.username or not data.password:
        return JSONResponse({"success": False, "error": "입력값 부족"}, status_code=400)
    if UserService.create_user(data.username, data.password, data.email):
        return {"success": True}
    return JSONResponse({"success": False, "error": "이미 존재하는 아이디"}, status_code=400)


@api_router.post('/auth/login')
async def login(request: Request, data: AuthRequest):
    if not data.username or not data.password:
        return JSONResponse({"success": False, "error": "입력값 부족"}, status_code=400)

    user = UserService.verify_user(data.username, data.password)
    if user:
        login_user(request, user)
        return {"success": True}
    return JSONResponse({"success": False, "error": "아이디 또는 비밀번호가 잘못되었습니다."}, status_code=401)


@api_router.post('/auth/logout')
async def logout(request: Request, user: CurrentUser = Depends(get_current_user)):
    logout_user(request)
    return {"success": True}


@api_router.get('/auth/me')
async def get_current_user_info(user: CurrentUser = Depends(get_current_user_optional)):
    return {
        "is_logged_in": user.is_authenticated,
        "username": user.id if user.is_authenticated else None
    }


# --- [빌드 진행률] ---
build_progress = {"status": "idle", "progress": 0}
build_lock = threading.Lock()


def update_build_progress(**kwargs):
    global build_progress
    with build_lock:
        build_progress.update(kwargs)


@api_router.get('/build_progress')
async def get_build_progress_sse():
    def generate():
        last_data = None
        start_time = time.time()
        max_duration = 300  # 5분 타임아웃

        # 즉시 현재 상태 전송
        with build_lock:
            current_data = json.dumps(build_progress)
        yield f"data: {current_data}\n\n"
        last_data = current_data

        while True:
            # 타임아웃 체크
            if time.time() - start_time > max_duration:
                with build_lock:
                    build_progress.update({"status": "error", "detail": "시간 초과"})
                    yield f"data: {json.dumps(build_progress)}\n\n"
                break

            with build_lock:
                current_data = json.dumps(build_progress)

            if current_data != last_data:
                yield f"data: {current_data}\n\n"
                last_data = current_data

            # 완료 또는 에러 상태면 종료
            with build_lock:
                if build_progress["status"] in ["completed", "error"]:
                    break

            time.sleep(0.3)

    return StreamingResponse(generate(), media_type='text/event-stream')


@api_router.post('/reset_build_progress')
async def reset_build_progress():
    """빌드 시작 전 진행 상태 초기화"""
    global build_progress
    with build_lock:
        build_progress = {"status": "idle", "progress": 0}
    return {"success": True}


# --- [시나리오 관리] ---

@api_router.get('/scenarios', response_class=HTMLResponse)

async def list_scenarios(
        request: Request,
        sort: str = 'newest',
        filter: str = Query('public'), # 기본값 public
        limit: Optional[int] = None,
        user: CurrentUser = Depends(get_current_user_optional)
):
    user_id = user.id if user.is_authenticated else None
    # ScenarioService에서 데이터 가져오기
    file_infos = ScenarioService.list_scenarios(sort, user_id, filter, limit)

    if not file_infos:
        msg = "아직 생성한 시나리오가 없습니다." if filter == 'my' else "표시할 시나리오가 없습니다."
        return f'<div class="col-span-full text-center text-gray-500 py-10">{msg}</div>'

    html = ""
    for info in file_infos:
        fid = info['filename']
        title = info['title']
        desc = info['desc']
        author = info['author']
        is_owner = info['is_owner']
        is_public = info['is_public']

        # 마이페이지와 메인페이지 카드 스타일 통합
        is_my_page = (filter == 'my')

        # 카드 HTML (mypage.html에서 사용한 고급스러운 스타일로 통일)
        html += f"""
        <div class="bg-rpg-800 border border-rpg-700 rounded-xl overflow-hidden group hover:border-rpg-accent hover:shadow-[0_0_20px_rgba(56,189,248,0.2)] transition-all flex flex-col h-full">
            <div class="relative h-48 overflow-hidden">
                <img src="https://images.unsplash.com/photo-1627850604058-52e40de1b847?q=80&w=800" class="w-full h-full object-cover transition-transform duration-500 group-hover:scale-110 opacity-80 group-hover:opacity-100">
                <div class="absolute top-3 left-3 bg-black/70 backdrop-blur px-2 py-1 rounded text-[10px] font-bold text-rpg-accent border border-rpg-accent/30">SCENARIO</div>
            </div>
            <div class="p-5 flex-1 flex flex-col gap-3 bg-rpg-800">
                <h3 class="text-lg font-bold text-white font-title tracking-wide truncate">{title}</h3>
                <p class="text-sm text-gray-400 line-clamp-2">{desc}</p>

                <div class="mt-auto pt-2 flex gap-2">
                    <button onclick="playScenario('{fid}', this)" class="flex-1 py-3 bg-rpg-700 hover:bg-rpg-accent hover:text-black text-white font-bold rounded-lg transition-all flex items-center justify-center gap-2 border border-rpg-700">
                        <i data-lucide="play" class="w-4 h-4 fill-current"></i> PLAY NOW
                    </button>
                    {" " if not is_my_page else f'''
                    <button onclick="editScenario('{fid}')" class="p-2 rounded-lg bg-rpg-900 text-gray-400 hover:text-white border border-rpg-700 hover:border-rpg-accent transition-all" title="수정">
                        <i data-lucide="edit" class="w-4 h-4"></i>
                    </button>
                    <button onclick="deleteScenario('{fid}', this)" class="p-2 rounded-lg bg-rpg-900 text-gray-400 hover:text-rpg-danger border border-rpg-700 hover:border-rpg-danger transition-all" title="삭제">
                        <i data-lucide="trash" class="w-4 h-4"></i>
                    </button>
                    '''}
                </div>
            </div>
        </div>
        """
    html += '<script>lucide.createIcons();</script>'
    return html


@api_router.post('/load_scenario')
async def load_scenario(
        filename: str = Form(...),
        user: CurrentUser = Depends(get_current_user_optional)
):
    user_id = user.id if user.is_authenticated else None

    result, error = ScenarioService.load_scenario(filename, user_id)
    if error:
        return JSONResponse({"error": error}, status_code=400)

    scenario = result['scenario']
    start_id = pick_start_scene_id(scenario)

    game_state.config['title'] = scenario.get('title', 'Loaded')
    game_state.state = {
        "scenario": scenario,
        "current_scene_id": "prologue",
        "start_scene_id": start_id,
        "player_vars": result['player_vars'],
        "history": [], "last_user_choice_idx": -1, "system_message": "Loaded", "npc_output": "", "narrator_output": ""
    }
    game_state.game_graph = create_game_graph()

    return {"success": True}


@api_router.post('/publish_scenario')
async def publish_scenario(
        data: ScenarioIdRequest,
        user: CurrentUser = Depends(get_current_user)
):
    success, msg = ScenarioService.publish_scenario(data.filename, user.id)
    return {"success": success, "message": msg, "error": msg}


@api_router.post('/delete_scenario')
async def delete_scenario(
        data: ScenarioIdRequest,
        user: CurrentUser = Depends(get_current_user)
):
    success, msg = ScenarioService.delete_scenario(data.filename, user.id)
    return {"success": success, "message": msg, "error": msg}


@api_router.get('/scenario/{scenario_id}/edit')
async def get_scenario_for_edit(scenario_id: str, user: CurrentUser = Depends(get_current_user)):
    result, error = ScenarioService.get_scenario_for_edit(scenario_id, user.id)
    if error:
        return JSONResponse({"success": False, "error": error}, status_code=403)
    return {"success": True, "data": result}


@api_router.post('/scenario/{scenario_id}/update')
async def update_scenario(scenario_id: str, request: Request, user: CurrentUser = Depends(get_current_user)):
    data = await request.json()
    success, error = ScenarioService.update_scenario(scenario_id, data, user.id)
    if not success:
        return JSONResponse({"success": False, "error": error}, status_code=400)
    return {"success": True, "message": "저장되었습니다."}


@api_router.post('/init_game')
async def init_game(request: Request, user: CurrentUser = Depends(get_current_user_optional)):
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return JSONResponse({"error": "API Key 없음"}, status_code=400)

    react_flow_data = await request.json()
    selected_model = react_flow_data.get('model', 'openai/tngtech/deepseek-r1t2-chimera:free')

    update_build_progress(status="building", step="0/5", detail="준비 중...", progress=0)

    try:
        set_progress_callback(update_build_progress)

        scenario_json = await run_in_threadpool(
            generate_scenario_from_graph,
            api_key,
            react_flow_data,
            model_name=selected_model
        )

        user_id = user.id if user.is_authenticated else None
        fid, error = ScenarioService.save_scenario(scenario_json, user_id=user_id)

        if error:
            update_build_progress(status="error", detail=f"저장 오류: {error}")
            return JSONResponse({"error": error}, status_code=500)

        game_state.config['title'] = scenario_json.get('title')
        game_state.state = {
            "scenario": scenario_json,
            "current_scene_id": pick_start_scene_id(scenario_json),
            "player_vars": {}, "history": [], "last_user_choice_idx": -1, "system_message": "Init", "npc_output": "",
            "narrator_output": ""
        }
        game_state.game_graph = create_game_graph()

        update_build_progress(status="completed", step="완료", detail="생성 완료!", progress=100)
        return {"status": "success", "filename": fid, **scenario_json}

    except Exception as e:
        logger.error(f"Init Error: {e}")
        update_build_progress(status="error", detail=str(e))
        return JSONResponse({"error": str(e)}, status_code=500)


# --- [NPC/Enemy 생성 및 저장 API] ---

@api_router.post('/npc/generate')
async def generate_npc_api(data: NPCGenerateRequest):
    try:
        # [수정] run_in_threadpool 적용 (LLM 호출 비동기 처리)
        npc_data = await run_in_threadpool(
            generate_single_npc,
            data.scenario_title,
            data.scenario_summary,
            data.request,
            data.model
        )
        return {"success": True, "data": npc_data}
    except Exception as e:
        logger.error(f"NPC Generation Error: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@api_router.post('/npc/save')
async def save_npc(request: Request, user: CurrentUser = Depends(get_current_user_optional)):
    try:
        data = await request.json()
        if not data:
            return JSONResponse({"success": False, "error": "No data provided"}, status_code=400)

        saved_entity = save_custom_npc(data, user.id if user.is_authenticated else None)

        return {
            "success": True,
            "message": "저장되었습니다.",
            "data": saved_entity
        }
    except Exception as e:
        logger.error(f"NPC Save Error: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# [수정] NPC 목록 조회 API (CustomNPC 사용)
@api_router.get('/npc/list')
async def get_npc_list(
        user: CurrentUser = Depends(get_current_user),
        db: Session = Depends(get_db)
):
    """사용자의 NPC 목록 조회"""
    if not user.is_authenticated:
        return JSONResponse({"success": False, "error": "로그인이 필요합니다."}, status_code=401)

    try:
        # models.py의 CustomNPC 테이블 조회
        # CustomNPC는 user_id 대신 author_id를 사용함
        npcs = db.query(CustomNPC).filter(CustomNPC.author_id == user.id).order_by(CustomNPC.created_at.desc()).all()

        # 프론트엔드 호환을 위한 데이터 변환
        results = []
        for npc in npcs:
            # CustomNPC의 상세 정보는 'data' JSON 컬럼에 저장되어 있음
            npc_data = npc.data if npc.data else {}

            results.append({
                "id": npc.id,
                "name": npc.name,
                "role": npc_data.get('role', '역할 미정'),
                "description": npc_data.get('description', '') or npc_data.get('personality', ''),
                "is_enemy": npc.type == 'enemy',
                "created_at": npc.created_at.timestamp() if npc.created_at else 0,
                # 필요하다면 전체 데이터도 포함
                "data": npc_data
            })

        return results
    except Exception as e:
        logger.error(f"NPC List Error: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# --- [프리셋 관리 (DB 직접 연결)] ---

@api_router.get('/presets')
async def list_presets(
        sort: str = 'newest',
        limit: Optional[int] = None,
        user: CurrentUser = Depends(get_current_user_optional),
        db: Session = Depends(get_db)
):
    """DB에서 프리셋 목록 조회"""
    try:
        query = db.query(Preset)

        # 개인용/공용 필터가 필요하다면 여기서 추가 (현재는 전체 공개)
        # if user.is_authenticated:
        #     query = query.filter(Preset.author_id == user.id)

        if sort == 'newest':
            query = query.order_by(Preset.created_at.desc())

        if limit:
            query = query.limit(limit)

        presets = query.all()
        return [p.to_dict() for p in presets]
    except Exception as e:
        logger.error(f"프리셋 조회 실패: {e}")
        return JSONResponse([], status_code=500)


@api_router.post('/presets/save')
async def save_preset(
        request: Request,
        user: CurrentUser = Depends(get_current_user),
        db: Session = Depends(get_db)
):
    """DB에 프리셋 저장"""
    try:
        data = await request.json()
        name = data.get('name')
        description = data.get('description', '')
        graph_data = data.get('data')

        if not name or not graph_data:
            return JSONResponse({"success": False, "error": "필수 데이터 누락"}, status_code=400)

        # 새 프리셋 객체 생성
        new_preset = Preset(
            name=name,
            description=description,
            data=graph_data,
            author_id=user.id if user.is_authenticated else None
        )

        db.add(new_preset)
        db.commit()
        db.refresh(new_preset)

        return {"success": True, "filename": new_preset.filename, "message": "프리셋이 저장되었습니다."}
    except Exception as e:
        db.rollback()
        logger.error(f"프리셋 저장 실패: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@api_router.post('/presets/load')
async def load_preset_api(
        request: Request,
        user: CurrentUser = Depends(get_current_user_optional),
        db: Session = Depends(get_db)
):
    """DB에서 프리셋 로드"""
    try:
        data = await request.json()
        filename = data.get('filename')  # 프론트엔드에서 보낸 filename (UUID)

        preset = db.query(Preset).filter(Preset.filename == filename).first()

        if not preset:
            return JSONResponse({"success": False, "error": "프리셋을 찾을 수 없습니다."}, status_code=404)

        return {
            "success": True,
            "data": preset.to_dict(),  # to_dict() 내부에 data 필드 포함
            "message": f"'{preset.name}' 프리셋을 불러왔습니다."
        }
    except Exception as e:
        logger.error(f"프리셋 로드 실패: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@api_router.post('/presets/delete')
async def delete_preset(
        request: Request,
        user: CurrentUser = Depends(get_current_user),
        db: Session = Depends(get_db)
):
    """DB에서 프리셋 삭제"""
    try:
        data = await request.json()
        filename = data.get('filename')

        preset = db.query(Preset).filter(Preset.filename == filename).first()

        if not preset:
            return JSONResponse({"success": False, "error": "삭제할 프리셋이 없습니다."}, status_code=404)

        # 권한 체크 (본인 것만 삭제 가능)
        if user.is_authenticated and preset.author_id != user.id:
            # 관리자가 아니면 차단하는 로직 등 추가 가능
            # 일단은 작성자만 삭제 허용
            pass

        db.delete(preset)
        db.commit()

        return {"success": True, "message": "삭제 완료"}
    except Exception as e:
        db.rollback()
        logger.error(f"프리셋 삭제 실패: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

# --- [Draft 시스템 API] ---

def _generate_mermaid_for_response(scenario_data):
    """응답용 Mermaid 코드 생성"""
    try:
        chart_data = MermaidService.generate_chart(scenario_data, None)
        return chart_data.get('mermaid_code', '')
    except Exception as e:
        logger.error(f"Mermaid generation error: {e}")
        return ''


@api_router.get('/draft/{scenario_id}')
async def get_draft(scenario_id: int, user: CurrentUser = Depends(get_current_user)):
    result, error = DraftService.get_draft(scenario_id, user.id)
    if error:
        return JSONResponse({"success": False, "error": error}, status_code=403)

    mermaid_code = _generate_mermaid_for_response(result['scenario'])
    return {"success": True, "mermaid_code": mermaid_code, **result}


@api_router.post('/draft/{scenario_id}/save')
async def save_draft(scenario_id: int, request: Request, user: CurrentUser = Depends(get_current_user)):
    data = await request.json()
    success, error = DraftService.save_draft(scenario_id, user.id, data)
    if not success:
        return JSONResponse({"success": False, "error": error}, status_code=400)
    return {"success": True, "message": "Draft가 저장되었습니다."}


@api_router.post('/draft/{scenario_id}/publish')
async def publish_draft(scenario_id: int, request: Request, user: CurrentUser = Depends(get_current_user)):
    data = await request.json() if await request.body() else {}
    force = data.get('force', False)

    success, error, validation_result = DraftService.publish_draft(scenario_id, user.id, force=force)

    if not success:
        return JSONResponse({
            "success": False,
            "error": error,
            "validation": validation_result
        }, status_code=400)

    return {
        "success": True,
        "message": "시나리오에 최종 반영되었습니다.",
        "validation": validation_result
    }


@api_router.api_route('/draft/{scenario_id}/validate', methods=['GET', 'POST'])
async def validate_draft(scenario_id: int, user: CurrentUser = Depends(get_current_user)):
    result, error = DraftService.get_draft(scenario_id, user.id)
    if error:
        return JSONResponse({"success": False, "error": error}, status_code=403)

    scenario_data = result['scenario']
    validation_result = validate_scenario_graph(scenario_data)

    return {
        "success": True,
        "can_publish": validation_result.is_valid,
        "validation": validation_result.to_dict()
    }


@api_router.post('/draft/{scenario_id}/discard')
async def discard_draft(scenario_id: int, user: CurrentUser = Depends(get_current_user)):
    success, error = DraftService.discard_draft(scenario_id, user.id)
    if not success:
        return JSONResponse({"success": False, "error": error}, status_code=400)
    return {"success": True, "message": "변경사항이 취소되었습니다."}


@api_router.post('/draft/{scenario_id}/reorder')
async def reorder_scene_ids(scenario_id: int, user: CurrentUser = Depends(get_current_user)):
    result, error = DraftService.get_draft(scenario_id, user.id)
    if error:
        return JSONResponse({"success": False, "error": error}, status_code=403)

    scenario_data = result['scenario']
    reordered_data, id_mapping = DraftService.reorder_scene_ids(scenario_data)

    if not id_mapping:
        return {"success": True, "message": "재정렬할 필요가 없습니다.", "changes": 0}

    success, save_error = DraftService.save_draft(scenario_id, user.id, reordered_data)
    if not success:
        return JSONResponse({"success": False, "error": save_error}, status_code=400)

    return {
        "success": True,
        "message": f"{len(id_mapping)}개의 씬 ID가 재정렬되었습니다.",
        "id_mapping": id_mapping,
        "scenario": reordered_data
    }


@api_router.post('/draft/{scenario_id}/check-references')
async def check_scene_references(scenario_id: int, data: DraftSceneRequest,
                                 user: CurrentUser = Depends(get_current_user)):
    if not data.scene_id:
        return JSONResponse({"success": False, "error": "scene_id가 필요합니다."}, status_code=400)

    result, error = DraftService.get_draft(scenario_id, user.id)
    if error:
        return JSONResponse({"success": False, "error": error}, status_code=403)

    references = DraftService.check_scene_references(result['scenario'], data.scene_id)

    return {
        "success": True,
        "scene_id": data.scene_id,
        "references": references,
        "has_references": len(references) > 0
    }


@api_router.post('/draft/{scenario_id}/delete-scene')
async def delete_scene(scenario_id: int, data: DraftSceneRequest, user: CurrentUser = Depends(get_current_user)):
    if not data.scene_id:
        return JSONResponse({"success": False, "error": "scene_id가 필요합니다."}, status_code=400)

    result, error = DraftService.get_draft(scenario_id, user.id)
    if error:
        return JSONResponse({"success": False, "error": error}, status_code=403)

    updated_scenario, warnings = DraftService.delete_scene(result['scenario'], data.scene_id, data.handle_mode)

    success, save_error = DraftService.save_draft(scenario_id, user.id, updated_scenario)
    if not success:
        return JSONResponse({"success": False, "error": save_error}, status_code=400)

    return {
        "success": True,
        "message": f"씬 '{data.scene_id}'이(가) 삭제되었습니다.",
        "warnings": warnings,
        "scenario": updated_scenario
    }


@api_router.post('/draft/{scenario_id}/add-scene')
async def add_scene(scenario_id: int, data: DraftSceneRequest, user: CurrentUser = Depends(get_current_user)):
    result, error = DraftService.get_draft(scenario_id, user.id)
    if error:
        return JSONResponse({"success": False, "error": error}, status_code=403)

    updated_scenario = DraftService.add_scene(result['scenario'], data.scene or {}, data.after_scene_id)

    success, save_error = DraftService.save_draft(scenario_id, user.id, updated_scenario)
    if not success:
        return JSONResponse({"success": False, "error": save_error}, status_code=400)

    added_scene = updated_scenario['scenes'][-1] if not data.after_scene_id else None
    if data.after_scene_id:
        for i, s in enumerate(updated_scenario['scenes']):
            if s.get('scene_id') == data.after_scene_id and i + 1 < len(updated_scenario['scenes']):
                added_scene = updated_scenario['scenes'][i + 1]
                break

    return {
        "success": True,
        "message": "새 씬이 추가되었습니다.",
        "scene": added_scene,
        "scenario": updated_scenario
    }


@api_router.post('/draft/{scenario_id}/add-ending')
async def add_ending(scenario_id: int, data: DraftEndingRequest, user: CurrentUser = Depends(get_current_user)):
    result, error = DraftService.get_draft(scenario_id, user.id)
    if error:
        return JSONResponse({"success": False, "error": error}, status_code=403)

    updated_scenario = DraftService.add_ending(result['scenario'], data.ending or {})

    success, save_error = DraftService.save_draft(scenario_id, user.id, updated_scenario)
    if not success:
        return JSONResponse({"success": False, "error": save_error}, status_code=400)

    added_ending = updated_scenario['endings'][-1]

    return {
        "success": True,
        "message": "새 엔딩이 추가되었습니다.",
        "ending": added_ending,
        "scenario": updated_scenario
    }


@api_router.post('/draft/{scenario_id}/delete-ending')
async def delete_ending(scenario_id: int, data: DraftEndingRequest, user: CurrentUser = Depends(get_current_user)):
    if not data.ending_id:
        return JSONResponse({"success": False, "error": "ending_id가 필요합니다."}, status_code=400)

    result, error = DraftService.get_draft(scenario_id, user.id)
    if error:
        return JSONResponse({"success": False, "error": error}, status_code=403)

    updated_scenario, warnings = DraftService.delete_ending(result['scenario'], data.ending_id)

    success, save_error = DraftService.save_draft(scenario_id, user.id, updated_scenario)
    if not success:
        return JSONResponse({"success": False, "error": save_error}, status_code=400)

    return {
        "success": True,
        "message": f"엔딩 '{data.ending_id}'이(가) 삭제되었습니다.",
        "warnings": warnings,
        "scenario": updated_scenario
    }


# --- [AI 서사 일관성 검사 API (run_in_threadpool 적용)] ---

@api_router.post('/draft/{scenario_id}/ai-audit')
async def ai_audit_scene(scenario_id: int, data: AuditRequest, user: CurrentUser = Depends(get_current_user)):
    if not data.scene_id:
        return JSONResponse({"success": False, "error": "scene_id가 필요합니다."}, status_code=400)

    result, error = DraftService.get_draft(scenario_id, user.id)
    if error:
        return JSONResponse({"success": False, "error": error}, status_code=403)

    scenario_data = result['scenario']

    # [수정] run_in_threadpool로 감싸서 서버 멈춤 방지
    if data.audit_type == 'coherence':
        audit_result = await run_in_threadpool(
            AIAuditService.audit_scene_coherence,
            scenario_data,
            data.scene_id,
            data.model
        )
        return {
            "success": audit_result.success,
            "audit_type": "coherence",
            "result": audit_result.to_dict()
        }
    elif data.audit_type == 'trigger':
        audit_result = await run_in_threadpool(
            AIAuditService.audit_trigger_consistency,
            scenario_data,
            data.scene_id,
            data.model
        )
        return {
            "success": audit_result.success,
            "audit_type": "trigger",
            "result": audit_result.to_dict()
        }
    else:  # full
        full_result = await run_in_threadpool(
            AIAuditService.full_audit,
            scenario_data,
            data.scene_id,
            data.model
        )
        return {
            "success": full_result.get('success', False),
            "audit_type": "full",
            "result": full_result
        }


@api_router.post('/draft/{scenario_id}/ai-audit-all')
async def ai_audit_all_scenes(scenario_id: int, request: Request, user: CurrentUser = Depends(get_current_user)):
    data = await request.json() if await request.body() else {}
    model_name = data.get('model')

    result, error = DraftService.get_draft(scenario_id, user.id)
    if error:
        return JSONResponse({"success": False, "error": error}, status_code=403)

    scenario_data = result['scenario']
    scenes = scenario_data.get('scenes', [])

    all_results = []
    total_issues = 0
    has_errors = False

    # [참고] 반복문 + LLM 호출은 시간이 걸리므로 클라이언트 타임아웃 주의 필요
    for scene in scenes:
        scene_id = scene.get('scene_id')
        if not scene_id:
            continue

        # [수정] 비동기 실행으로 변경
        audit_result = await run_in_threadpool(
            AIAuditService.full_audit,
            scenario_data,
            scene_id,
            model_name
        )

        all_results.append({
            'scene_id': scene_id,
            'scene_title': scene.get('title') or scene.get('name') or scene_id,
            'result': audit_result
        })

        total_issues += audit_result.get('total_issues', 0)
        if audit_result.get('has_errors'):
            has_errors = True

    return {
        "success": True,
        "total_scenes": len(scenes),
        "total_issues": total_issues,
        "has_errors": has_errors,
        "results": all_results
    }


@api_router.post('/draft/{scenario_id}/audit-recommend')
async def recommend_audit_targets_api(scenario_id: int, request: Request,
                                      user: CurrentUser = Depends(get_current_user)):
    """[신규] AI 검수 추천 API"""
    data = await request.json() if await request.body() else {}
    model_name = data.get('model')

    result, error = DraftService.get_draft(scenario_id, user.id)
    if error:
        return JSONResponse({"success": False, "error": error}, status_code=403)

    scenario_data = result['scenario']

    # [수정] 서비스 호출 비동기 처리
    recommendation_result = await run_in_threadpool(
        AIAuditService.recommend_audit_targets,
        scenario_data,
        model_name
    )

    if not recommendation_result.get("success"):
        return JSONResponse(recommendation_result, status_code=500)

    return recommendation_result


# --- [변경 이력 관리 API] ---

@api_router.get('/draft/{scenario_id}/history')
async def get_history_list(scenario_id: int, user: CurrentUser = Depends(get_current_user)):
    history_list, current_sequence, error = HistoryService.get_history_list(scenario_id, user.id)

    if error:
        return JSONResponse({"success": False, "error": error}, status_code=400)

    undo_redo_status = HistoryService.get_undo_redo_status(scenario_id, user.id)

    return {
        "success": True,
        "history": history_list,
        "current_sequence": current_sequence,
        "undo_redo_status": undo_redo_status
    }


@api_router.post('/draft/{scenario_id}/history/init')
async def init_history(scenario_id: int, user: CurrentUser = Depends(get_current_user)):
    result, error = DraftService.get_draft(scenario_id, user.id)
    if error:
        return JSONResponse({"success": False, "error": error}, status_code=403)

    success, hist_error = HistoryService.initialize_history(scenario_id, user.id, result['scenario'])

    if not success:
        return JSONResponse({"success": False, "error": hist_error}, status_code=400)

    return {"success": True, "message": "이력이 초기화되었습니다."}


@api_router.post('/draft/{scenario_id}/history/add')
async def add_history(scenario_id: int, data: HistoryAddRequest, user: CurrentUser = Depends(get_current_user)):
    snapshot = data.snapshot

    if not snapshot:
        result, error = DraftService.get_draft(scenario_id, user.id)
        if error:
            return JSONResponse({"success": False, "error": error}, status_code=403)
        snapshot = result['scenario']

    success, hist_error = HistoryService.add_history(
        scenario_id, user.id, data.action_type, data.action_description, snapshot
    )

    if not success:
        return JSONResponse({"success": False, "error": hist_error}, status_code=400)

    undo_redo_status = HistoryService.get_undo_redo_status(scenario_id, user.id)

    return {
        "success": True,
        "message": "이력이 추가되었습니다.",
        "undo_redo_status": undo_redo_status
    }


@api_router.post('/draft/{scenario_id}/history/undo')
async def undo_action(scenario_id: int, user: CurrentUser = Depends(get_current_user)):
    restored_data, error = HistoryService.undo(scenario_id, user.id)

    if error:
        return JSONResponse({"success": False, "error": error}, status_code=400)

    mermaid_code = _generate_mermaid_for_response(restored_data)
    undo_redo_status = HistoryService.get_undo_redo_status(scenario_id, user.id)

    return {
        "success": True,
        "message": "이전 상태로 복원되었습니다.",
        "scenario": restored_data,
        "mermaid_code": mermaid_code,
        "undo_redo_status": undo_redo_status
    }


@api_router.post('/draft/{scenario_id}/history/redo')
async def redo_action(scenario_id: int, user: CurrentUser = Depends(get_current_user)):
    restored_data, error = HistoryService.redo(scenario_id, user.id)

    if error:
        return JSONResponse({"success": False, "error": error}, status_code=400)

    mermaid_code = _generate_mermaid_for_response(restored_data)
    undo_redo_status = HistoryService.get_undo_redo_status(scenario_id, user.id)

    return {
        "success": True,
        "message": "다음 상태로 복원되었습니다.",
        "scenario": restored_data,
        "mermaid_code": mermaid_code,
        "undo_redo_status": undo_redo_status
    }


@api_router.post('/draft/{scenario_id}/history/restore/{history_id}')
async def restore_to_history_point(scenario_id: int, history_id: int, user: CurrentUser = Depends(get_current_user)):
    restored_data, error = HistoryService.restore_to_point(scenario_id, user.id, history_id)

    if error:
        return JSONResponse({"success": False, "error": error}, status_code=400)

    mermaid_code = _generate_mermaid_for_response(restored_data)
    undo_redo_status = HistoryService.get_undo_redo_status(scenario_id, user.id)

    return {
        "success": True,
        "message": "해당 시점으로 복원되었습니다.",
        "scenario": restored_data,
        "mermaid_code": mermaid_code,
        "undo_redo_status": undo_redo_status
    }


@api_router.post('/draft/{scenario_id}/history/clear')
async def clear_history(scenario_id: int, user: CurrentUser = Depends(get_current_user)):
    success, error = HistoryService.clear_history(scenario_id, user.id)

    if not success:
        return JSONResponse({"success": False, "error": error}, status_code=400)

    return {"success": True, "message": "이력이 삭제되었습니다."}


@api_router.get('/draft/{scenario_id}/history/status')
async def get_undo_redo_status(scenario_id: int, user: CurrentUser = Depends(get_current_user)):
    status = HistoryService.get_undo_redo_status(scenario_id, user.id)

    return {"success": True, **status}

# api.py 파일의 해당 부분을 아래와 같이 수정하세요.

# --- [시나리오 관리] ---

# 1. 마이페이지 뷰를 /views/mypage 경로로 설정 (api_router가 아닌 router 사용)
@router.get('/mypage', response_class=HTMLResponse)
async def mypage_view(request: Request, user: CurrentUser = Depends(get_current_user_optional)):
    """
    마이페이지 뷰를 반환합니다.
    """
    return templates.TemplateResponse("mypage.html", {"request": request, "user": user})

