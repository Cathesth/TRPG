import os
import json
import logging
import time
import threading
import glob  # <--- 이 줄을 추가해주세요!
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, APIRouter, Request, Depends, Form, HTTPException, Query
from fastapi.responses import JSONResponse, HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from starlette.concurrency import run_in_threadpool

# 빌더 에이전트 및 코어 유틸리티
from builder_agent import generate_scenario_from_graph, set_progress_callback, generate_single_npc
from core.state import game_state
from core.utils import parse_request_data, pick_start_scene_id, validate_scenario_graph, can_publish_scenario
from game_engine import create_game_graph

# 서비스 계층 임포트
from services.scenario_service import ScenarioService
from services.user_service import UserService
from services.draft_service import DraftService
from services.ai_audit_service import AIAuditService
from services.history_service import HistoryService
from services.npc_service import save_custom_npc
from services.mermaid_service import MermaidService

# 인증 및 모델
from routes.auth import get_current_user, get_current_user_optional, login_user, logout_user, CurrentUser
from models import get_db, Preset, CustomNPC, Scenario

# 로깅 설정
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="templates")

# 라우터 정의
mypage_router = APIRouter(prefix="/views", tags=["views"])
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


# ==========================================
# [View 라우트] 마이페이지
# ==========================================
@mypage_router.get('/mypage', response_class=HTMLResponse)
async def mypage_view(request: Request, user: CurrentUser = Depends(get_current_user_optional)):
    return templates.TemplateResponse("mypage.html", {"request": request, "user": user})


# ==========================================
# [API 라우트] 인증 (Auth)
# ==========================================
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


# ==========================================
# [API 라우트] 빌드 진행률 (SSE)
# ==========================================
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

        with build_lock:
            current_data = json.dumps(build_progress)
        yield f"data: {current_data}\n\n"
        last_data = current_data

        while True:
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

            with build_lock:
                if build_progress["status"] in ["completed", "error"]:
                    break
            time.sleep(0.3)

    return StreamingResponse(generate(), media_type='text/event-stream')


@api_router.post('/reset_build_progress')
async def reset_build_progress():
    global build_progress
    with build_lock:
        build_progress = {"status": "idle", "progress": 0}
    return {"success": True}


# [교체] routes/api.py -> list_scenarios 함수
@api_router.get('/scenarios', response_class=HTMLResponse)
def list_scenarios(
        request: Request,
        sort: str = Query('newest'),
        filter: str = Query('public'),
        limit: int = Query(10),
        user: CurrentUser = Depends(get_current_user_optional),
        db: Session = Depends(get_db)
):
    """
    DB에서 시나리오를 조회하여 HTML 카드로 반환합니다.
    - 메인화면: w-64 (약 4개 보임) 유지
    - 마이페이지/플레이어: w-full + 고정 높이(h-[22rem])로 직사각형 비율 복구
    """

    # 1. DB 쿼리 생성
    query = db.query(Scenario)

    # 2. 필터링
    if filter == 'my':
        if not user.is_authenticated:
            return HTMLResponse('<div class="col-span-full text-center text-gray-500 py-10 w-full">로그인이 필요합니다.</div>')
        query = query.filter(Scenario.author_id == user.id)
    elif filter == 'public':
        query = query.filter(Scenario.is_public == True)
    # filter='all'은 전체 조회

    # 3. 정렬
    if sort == 'oldest':
        query = query.order_by(Scenario.created_at.asc())
    elif sort == 'name_asc':
        query = query.order_by(Scenario.title.asc())
    else:
        query = query.order_by(Scenario.created_at.desc())

    # 4. 데이터 조회
    if limit:
        query = query.limit(limit)

    scenarios = query.all()

    if not scenarios:
        msg = "등록된 시나리오가 없습니다." if filter != 'my' else "아직 생성한 시나리오가 없습니다."
        return HTMLResponse(
            f'<div class="col-span-full text-center text-gray-500 py-12 w-full flex flex-col items-center"><i data-lucide="inbox" class="w-10 h-10 mb-2 opacity-50"></i><p>{msg}</p></div>')

    # 5. HTML 생성
    from datetime import datetime
    import time as time_module
    current_ts = time_module.time()
    NEW_THRESHOLD = 30 * 60

    html = ""
    for s in scenarios:
        s_data = s.data if isinstance(s.data, dict) else {}
        if 'scenario' in s_data: s_data = s_data['scenario']

        fid = str(s.id)
        title = s.title or "제목 없음"
        desc = s_data.get('prologue', s_data.get('desc', '설명이 없습니다.'))
        if len(desc) > 60: desc = desc[:60] + "..."

        author = s.author_id or "System"
        is_owner = (user.is_authenticated and s.author_id == user.id)
        is_public = s.is_public

        created_ts = s.created_at.timestamp() if s.created_at else 0
        time_str = s.created_at.strftime('%Y-%m-%d') if s.created_at else "-"

        img_src = s_data.get('image') or "https://images.unsplash.com/photo-1519074069444-1ba4fff66d16?q=80&w=800"

        is_new = (current_ts - created_ts) < NEW_THRESHOLD
        new_badge = '<span class="ml-2 text-[10px] bg-red-500 text-white px-1.5 py-0.5 rounded-full font-bold animate-pulse">NEW</span>' if is_new else ''

        # [디자인 분기]
        if filter == 'my':
            # [수정됨] 마이페이지 & 플레이어 화면 (저장된 목록)
            # - aspect-square 제거: 카드가 너무 길어지는 문제 해결
            # - h-[22rem]: 높이를 고정하여 직사각형 형태 유지
            # - w-full: 그리드 칸에 맞춰 가로로 꽉 참
            card_style = "w-full h-[22rem]"
            img_height = "h-44"  # 이미지 높이 고정 (176px)
            content_padding = "p-4"
        else:
            # [유지] 메인 화면
            # - w-64: 4개 배치 최적화
            card_style = "w-64 h-[20rem] flex-shrink-0 snap-center"
            img_height = "h-40"
            content_padding = "p-4"

        # [버튼 구성]
        if is_owner:
            buttons_html = f"""
            <div class="flex items-center gap-2 mt-auto pt-3 border-t border-white/10">
                <button onclick="playScenario('{fid}', this)" class="flex-1 py-2 bg-[#1e293b] hover:bg-[#38bdf8] hover:text-black text-white font-bold rounded-lg transition-all flex items-center justify-center gap-2 shadow-md border border-[#1e293b] text-xs">
                    <i data-lucide="play" class="w-3 h-3 fill-current"></i> PLAY
                </button>
                <button onclick="editScenario('{fid}', this)" class="p-2 rounded-lg bg-transparent hover:bg-white/10 text-gray-400 hover:text-[#38bdf8] transition-colors" title="수정">
                    <i data-lucide="edit" class="w-4 h-4"></i>
                </button>
                <button onclick="deleteScenario('{fid}', this)" class="p-2 rounded-lg bg-transparent hover:bg-red-500/10 text-gray-400 hover:text-red-500 transition-colors" title="삭제">
                    <i data-lucide="trash" class="w-4 h-4"></i>
                </button>
            </div>
            """
        else:
            buttons_html = f"""
            <div class="mt-auto pt-3 border-t border-white/10">
                <button onclick="playScenario('{fid}', this)" class="w-full py-2 bg-[#1e293b] hover:bg-[#38bdf8] hover:text-black text-white font-bold rounded-lg transition-all flex items-center justify-center gap-2 shadow-md border border-[#1e293b] text-xs">
                    <i data-lucide="play" class="w-3 h-3 fill-current"></i> PLAY NOW
                </button>
            </div>
            """

        # [카드 HTML 조립]
        card_html = f"""
        <div class="scenario-card-base group bg-[#0f172a] border border-[#1e293b] rounded-xl overflow-hidden hover:border-[#38bdf8] transition-all flex flex-col shadow-lg relative {card_style}">
            <div class="relative {img_height} overflow-hidden bg-black shrink-0">
                <img src="{img_src}" class="w-full h-full object-cover transition-transform duration-500 group-hover:scale-110 opacity-80 group-hover:opacity-100">
                <div class="absolute top-2 left-2 bg-black/70 backdrop-blur px-2 py-1 rounded text-[10px] font-bold text-[#38bdf8] border border-[#38bdf8]/30">
                    Fantasy
                </div>
            </div>

            <div class="{content_padding} flex-1 flex flex-col justify-between">
                <div>
                    <div class="flex justify-between items-start mb-1">
                        <h3 class="text-base font-bold text-white tracking-wide truncate w-full group-hover:text-[#38bdf8] transition-colors">{title} {new_badge}</h3>
                    </div>
                    <div class="flex justify-between items-center text-xs text-gray-400 mb-2">
                        <span>{author}</span>
                        <span class="flex items-center gap-1"><i data-lucide="clock" class="w-3 h-3"></i>{time_str}</span>
                    </div>
                    <p class="text-xs text-gray-400 line-clamp-2 leading-tight min-h-[2.5em]">{desc}</p>
                </div>

                {buttons_html}
            </div>
        </div>
        """
        html += card_html

    html += '<script>lucide.createIcons();</script>'
    return HTMLResponse(content=html)

@api_router.get('/scenarios/data')
async def get_scenarios_data(
        sort: str = 'newest',
        filter: str = 'my',
        user: CurrentUser = Depends(get_current_user)
):
    """빌더 모달용 JSON 응답 API"""
    user_id = user.id if user.is_authenticated else None
    file_infos = ScenarioService.list_scenarios(sort, user_id, filter)
    return file_infos


@api_router.post('/load_scenario')
async def load_scenario(
        filename: str = Form(...),
        user: CurrentUser = Depends(get_current_user_optional)
):
    import uuid
    from core.state import WorldState
    from routes.game import save_game_session

    user_id = user.id if user.is_authenticated else None
    result, error = ScenarioService.load_scenario(filename, user_id)
    if error:
        return JSONResponse({"error": error}, status_code=400)

    scenario = result['scenario']
    start_id = pick_start_scene_id(scenario)

    # ============================================
    # 🔥 새로운 세션 ID 생성 (기존 세션 완전히 무시)
    # ============================================
    new_session_key = str(uuid.uuid4())
    logger.info(f"🆕 [LOAD_SCENARIO] Creating new session: {new_session_key}")

    # ============================================
    # 🔄 GameState 완전 초기화
    # ============================================
    game_state.clear()  # 싱글톤 인스턴스 초기화
    game_state.config['title'] = scenario.get('title', 'Loaded')

    # [경량화] scenario 전체 대신 scenario_id만 저장
    scenario_id = scenario.get('id', 0)

    # ============================================
    # 🔄 WorldState 완전 초기화 (싱글톤 인스턴스 리셋)
    # ============================================
    world_state_instance = WorldState()
    world_state_instance.reset()  # 기존 데이터 완전 삭제
    world_state_instance.initialize_from_scenario(scenario)
    logger.info(f"🌍 [LOAD_SCENARIO] WorldState reset and initialized")

    # ============================================
    # 📝 새로운 player_state 생성
    # ============================================
    game_state.state = {
        "scenario_id": scenario_id,  # [경량화] ID만 저장
        "current_scene_id": "prologue",
        "start_scene_id": start_id,
        "player_vars": result['player_vars'],
        # [경량화] world_state 제거 - WorldState 싱글톤 인스턴스에서 관리
        # [경량화] history 제거 - WorldState에서 관리
        "last_user_choice_idx": -1,
        "last_user_input": "",
        "parsed_intent": "",
        "system_message": "Loaded",
        "npc_output": "",
        "narrator_output": "",
        "critic_feedback": "",
        "retry_count": 0,
        "chat_log_html": "",
        "near_miss_trigger": None,
        "model": "openai/tngtech/deepseek-r1t2-chimera:free",
        "_internal_flags": {}
    }
    game_state.game_graph = create_game_graph()

    # ============================================
    # 💾 DB에 새로운 세션 저장 (완전히 새로운 세션으로 강제)
    # ============================================
    db = next(get_db())
    try:
        saved_session_key = save_game_session(
            db=db,
            state=game_state.state.copy(),
            user_id=user_id,
            session_key=new_session_key  # 새로운 세션 키 강제 사용
        )
        logger.info(f"✅ [LOAD_SCENARIO] New session saved to DB: {saved_session_key}")
    except Exception as e:
        logger.error(f"❌ [LOAD_SCENARIO] Failed to save session: {e}")
        saved_session_key = new_session_key
    finally:
        db.close()

    # ============================================
    # 🎯 클라이언트에 새로운 세션 ID 반환 (이후 요청에서 사용)
    # ============================================
    return {
        "success": True,
        "session_key": saved_session_key,
        "message": "New game session created. Previous session data cleared."
    }


@api_router.post('/publish_scenario')
async def publish_scenario(data: ScenarioIdRequest, user: CurrentUser = Depends(get_current_user)):
    success, msg = ScenarioService.publish_scenario(data.filename, user.id)
    return {"success": success, "message": msg, "error": msg}


@api_router.post('/delete_scenario')
async def delete_scenario(data: ScenarioIdRequest, user: CurrentUser = Depends(get_current_user)):
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

        # [경량화] scenario 전체 대신 scenario_id만 저장
        scenario_id = scenario_json.get('id', 0)
        start_scene_id = pick_start_scene_id(scenario_json)

        # [FIX] WorldState 초기화
        from core.state import WorldState
        world_state_instance = WorldState()
        world_state_instance.reset()
        world_state_instance.initialize_from_scenario(scenario_json)

        # [경량화] player_state에는 world_state와 history를 포함하지 않음
        game_state.state = {
            "scenario_id": scenario_id,  # [경량화] ID만 저장
            "current_scene_id": start_scene_id,
            "start_scene_id": start_scene_id,
            "player_vars": {},
            # [경량화] world_state 제거 - WorldState 싱글톤 인스턴스에서 관리
            # [경량화] history 제거 - WorldState에서 관리
            "last_user_choice_idx": -1,
            "last_user_input": "",
            "parsed_intent": "",
            "system_message": "Init",
            "npc_output": "",
            "narrator_output": "",
            "critic_feedback": "",
            "retry_count": 0,
            "chat_log_html": "",
            "near_miss_trigger": None,
            "model": selected_model,
            "_internal_flags": {}
        }
        game_state.game_graph = create_game_graph()

        update_build_progress(status="completed", step="완료", detail="생성 완료!", progress=100)
        return {"status": "success", "filename": fid, **scenario_json}

    except Exception as e:
        logger.error(f"Init Error: {e}")
        update_build_progress(status="error", detail=str(e))
        return JSONResponse({"error": str(e)}, status_code=500)


# ==========================================
# [API 라우트] NPC 관리
# ==========================================
@api_router.post('/npc/generate')
async def generate_npc_api(data: NPCGenerateRequest):
    try:
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
        return {"success": True, "message": "저장되었습니다.", "data": saved_entity}
    except Exception as e:
        logger.error(f"NPC Save Error: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@api_router.get('/npc/list')
async def get_npc_list(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    if not user.is_authenticated:
        return JSONResponse({"success": False, "error": "로그인이 필요합니다."}, status_code=401)
    try:
        npcs = db.query(CustomNPC).filter(CustomNPC.author_id == user.id).order_by(CustomNPC.created_at.desc()).all()
        results = []
        for npc in npcs:
            npc_data = npc.data if npc.data else {}
            results.append({
                "id": npc.id,
                "name": npc.name,
                "role": npc_data.get('role', '역할 미정'),
                "description": npc_data.get('description', '') or npc_data.get('personality', ''),
                "is_enemy": npc.type == 'enemy',
                "created_at": npc.created_at.timestamp() if npc.created_at else 0,
                "data": npc_data
            })
        return results
    except Exception as e:
        logger.error(f"NPC List Error: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# ==========================================
# [API 라우트] 프리셋 관리
# ==========================================
@api_router.get('/presets')
async def list_presets(sort: str = 'newest', limit: Optional[int] = None, db: Session = Depends(get_db)):
    try:
        query = db.query(Preset)
        if sort == 'newest': query = query.order_by(Preset.created_at.desc())
        if limit: query = query.limit(limit)
        presets = query.all()
        return [p.to_dict() for p in presets]
    except Exception as e:
        logger.error(f"프리셋 조회 실패: {e}")
        return JSONResponse([], status_code=500)


@api_router.post('/presets/save')
async def save_preset(request: Request, user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        data = await request.json()
        name = data.get('name')
        description = data.get('description', '')
        graph_data = data.get('data')
        if not name or not graph_data:
            return JSONResponse({"success": False, "error": "필수 데이터 누락"}, status_code=400)

        new_preset = Preset(name=name, description=description, data=graph_data,
                            author_id=user.id if user.is_authenticated else None)
        db.add(new_preset)
        db.commit()
        db.refresh(new_preset)
        return {"success": True, "filename": new_preset.filename, "message": "프리셋이 저장되었습니다."}
    except Exception as e:
        db.rollback()
        logger.error(f"프리셋 저장 실패: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@api_router.post('/presets/load')
async def load_preset_api(request: Request, db: Session = Depends(get_db)):
    try:
        data = await request.json()
        filename = data.get('filename')
        preset = db.query(Preset).filter(Preset.filename == filename).first()
        if not preset: return JSONResponse({"success": False, "error": "프리셋을 찾을 수 없습니다."}, status_code=404)
        return {"success": True, "data": preset.to_dict(), "message": f"'{preset.name}' 프리셋을 불러왔습니다."}
    except Exception as e:
        logger.error(f"프리셋 로드 실패: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@api_router.post('/presets/delete')
async def delete_preset(request: Request, user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        data = await request.json()
        filename = data.get('filename')
        preset = db.query(Preset).filter(Preset.filename == filename).first()
        if not preset: return JSONResponse({"success": False, "error": "삭제할 프리셋이 없습니다."}, status_code=404)
        db.delete(preset)
        db.commit()
        return {"success": True, "message": "삭제 완료"}
    except Exception as e:
        db.rollback()
        logger.error(f"프리셋 삭제 실패: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@api_router.post('/load_preset')
async def load_preset_old(filename: str = Form(...), db: Session = Depends(get_db)):
    try:
        preset = db.query(Preset).filter(Preset.filename == filename).first()
        if not preset: return HTMLResponse('<div class="error">로드 실패</div>')
        game_state.config['title'] = preset.name
        return HTMLResponse(
            f'<div class="success">프리셋 로드 완료! "{preset.name}"</div><script>lucide.createIcons();</script>')
    except Exception as e:
        return HTMLResponse(f'<div class="error">로드 오류: {e}</div>')


# ==========================================
# [API 라우트] Draft 및 편집 시스템
# ==========================================

def _generate_mermaid_for_response(scenario_data):
    try:
        chart_data = MermaidService.generate_chart(scenario_data, None)
        return chart_data.get('mermaid_code', '')
    except Exception as e:
        logger.error(f"Mermaid generation error: {e}")
        return ''


@api_router.get('/draft/{scenario_id}')
async def get_draft(scenario_id: int, user: CurrentUser = Depends(get_current_user)):
    result, error = DraftService.get_draft(scenario_id, user.id)
    if error: return JSONResponse({"success": False, "error": error}, status_code=403)
    mermaid_code = _generate_mermaid_for_response(result['scenario'])
    return {"success": True, "mermaid_code": mermaid_code, **result}


@api_router.post('/draft/{scenario_id}/save')
async def save_draft(scenario_id: int, request: Request, user: CurrentUser = Depends(get_current_user)):
    data = await request.json()

    # [Fix] nodes만 있고 scenes가 없으면 자동 생성하여 함께 저장
    if 'nodes' in data and ('scenes' not in data or not data['scenes']):
        scenes, endings = MermaidService.convert_nodes_to_scenes(data.get('nodes', []), data.get('edges', []))
        data['scenes'] = scenes
        data['endings'] = endings

    success, error = DraftService.save_draft(scenario_id, user.id, data)
    if not success: return JSONResponse({"success": False, "error": error}, status_code=400)

    # 자동 히스토리 추가
    HistoryService.add_snapshot(scenario_id, user.id, data, "Draft 저장")
    return {"success": True, "message": "Draft가 저장되었습니다."}


@api_router.post('/draft/{scenario_id}/publish')
async def publish_draft(scenario_id: int, request: Request, user: CurrentUser = Depends(get_current_user)):
    data = await request.json() if await request.body() else {}
    force = data.get('force', False)
    success, error, validation_result = DraftService.publish_draft(scenario_id, user.id, force=force)
    if not success:
        return JSONResponse({"success": False, "error": error, "validation": validation_result}, status_code=400)
    return {"success": True, "message": "시나리오에 최종 반영되었습니다.", "validation": validation_result}


@api_router.post('/draft/{scenario_id}/discard')
async def discard_draft(scenario_id: int, user: CurrentUser = Depends(get_current_user)):
    success, error = DraftService.discard_draft(scenario_id, user.id)
    if not success: return JSONResponse({"success": False, "error": error}, status_code=400)
    return {"success": True, "message": "변경사항이 취소되었습니다."}


@api_router.post('/draft/{scenario_id}/reorder')
async def reorder_scene_ids(scenario_id: int, user: CurrentUser = Depends(get_current_user)):
    result, error = DraftService.get_draft(scenario_id, user.id)
    if error: return JSONResponse({"success": False, "error": error}, status_code=403)

    scenario_data = result['scenario']
    reordered_data, id_mapping = DraftService.reorder_scene_ids(scenario_data)

    if not id_mapping:
        return {"success": True, "message": "재정렬할 필요가 없습니다.", "changes": 0}

    success, save_error = DraftService.save_draft(scenario_id, user.id, reordered_data)
    if not success: return JSONResponse({"success": False, "error": save_error}, status_code=400)

    return {"success": True, "message": f"{len(id_mapping)}개의 씬 ID가 재정렬되었습니다.", "id_mapping": id_mapping,
            "scenario": reordered_data}


@api_router.post('/draft/{scenario_id}/check-references')
async def check_scene_references(scenario_id: int, data: DraftSceneRequest,
                                 user: CurrentUser = Depends(get_current_user)):
    if not data.scene_id: return JSONResponse({"success": False, "error": "scene_id 필요"}, status_code=400)
    result, error = DraftService.get_draft(scenario_id, user.id)
    if error: return JSONResponse({"success": False, "error": error}, status_code=403)
    references = DraftService.check_scene_references(result['scenario'], data.scene_id)
    return {"success": True, "scene_id": data.scene_id, "references": references, "has_references": len(references) > 0}


@api_router.post('/draft/{scenario_id}/add-scene')
async def add_scene(scenario_id: int, data: DraftSceneRequest, user: CurrentUser = Depends(get_current_user)):
    result, error = DraftService.get_draft(scenario_id, user.id)
    if error: return JSONResponse({"success": False, "error": error}, status_code=403)

    updated_scenario = DraftService.add_scene(result['scenario'], data.scene or {}, data.after_scene_id)
    success, save_error = DraftService.save_draft(scenario_id, user.id, updated_scenario)
    if not success: return JSONResponse({"success": False, "error": save_error}, status_code=400)

    # 추가된 씬 찾기
    added_scene = updated_scenario['scenes'][-1]
    return {"success": True, "message": "새 씬 추가됨", "scene": added_scene, "scenario": updated_scenario}


@api_router.post('/draft/{scenario_id}/add-ending')
async def add_ending(scenario_id: int, data: DraftEndingRequest, user: CurrentUser = Depends(get_current_user)):
    result, error = DraftService.get_draft(scenario_id, user.id)
    if error: return JSONResponse({"success": False, "error": error}, status_code=403)

    updated_scenario = DraftService.add_ending(result['scenario'], data.ending or {})
    success, save_error = DraftService.save_draft(scenario_id, user.id, updated_scenario)
    if not success: return JSONResponse({"success": False, "error": save_error}, status_code=400)

    added_ending = updated_scenario['endings'][-1]
    return {"success": True, "message": "새 엔딩 추가됨", "ending": added_ending, "scenario": updated_scenario}


@api_router.post('/draft/{scenario_id}/delete-scene')
async def delete_scene(scenario_id: int, data: DraftSceneRequest, user: CurrentUser = Depends(get_current_user)):
    if not data.scene_id: return JSONResponse({"success": False, "error": "scene_id 필요"}, status_code=400)
    result, error = DraftService.get_draft(scenario_id, user.id)
    if error: return JSONResponse({"success": False, "error": error}, status_code=403)

    updated_scenario, warnings = DraftService.delete_scene(result['scenario'], data.scene_id, data.handle_mode)
    success, save_error = DraftService.save_draft(scenario_id, user.id, updated_scenario)
    if not success: return JSONResponse({"success": False, "error": save_error}, status_code=400)

    return {"success": True, "message": "씬 삭제 완료", "warnings": warnings, "scenario": updated_scenario}


@api_router.post('/draft/{scenario_id}/delete-ending')
async def delete_ending(scenario_id: int, data: DraftEndingRequest, user: CurrentUser = Depends(get_current_user)):
    if not data.ending_id: return JSONResponse({"success": False, "error": "ending_id 필요"}, status_code=400)
    result, error = DraftService.get_draft(scenario_id, user.id)
    if error: return JSONResponse({"success": False, "error": error}, status_code=403)

    updated_scenario, warnings = DraftService.delete_ending(result['scenario'], data.ending_id)
    success, save_error = DraftService.save_draft(scenario_id, user.id, updated_scenario)
    if not success: return JSONResponse({"success": False, "error": save_error}, status_code=400)

    return {"success": True, "message": "엔딩 삭제 완료", "warnings": warnings, "scenario": updated_scenario}


# ==========================================
# [API 라우트] AI Audit & Recommendation
# ==========================================
@api_router.post('/draft/{scenario_id}/ai-audit')
async def ai_audit_scene(scenario_id: int, data: AuditRequest, user: CurrentUser = Depends(get_current_user)):
    if not data.scene_id: return JSONResponse({"success": False, "error": "scene_id 필요"}, status_code=400)
    result, error = DraftService.get_draft(scenario_id, user.id)
    if error: return JSONResponse({"success": False, "error": error}, status_code=403)

    # 비동기 실행으로 서버 블로킹 방지
    method = AIAuditService.full_audit
    if data.audit_type == 'coherence':
        method = AIAuditService.audit_scene_coherence
    elif data.audit_type == 'trigger':
        method = AIAuditService.audit_trigger_consistency

    audit_result = await run_in_threadpool(method, result['scenario'], data.scene_id, data.model)

    return {"success": True, "audit_type": data.audit_type, "result": audit_result}


@api_router.post('/draft/{scenario_id}/audit-recommend')
async def audit_recommend(scenario_id: int, request: Request, user: CurrentUser = Depends(get_current_user)):
    data = await request.json() if await request.body() else {}
    result, error = DraftService.get_draft(scenario_id, user.id)
    if error: return JSONResponse({"success": False, "error": error}, status_code=403)

    recommendation_result = await run_in_threadpool(AIAuditService.recommend_audit_targets, result['scenario'],
                                                    data.get('model'))
    if not recommendation_result.get("success"): return JSONResponse(recommendation_result, status_code=500)
    return recommendation_result


# ==========================================
# [API 라우트] History (Undo/Redo)
# ==========================================
@api_router.get('/draft/{scenario_id}/history')
async def get_history_list(scenario_id: int, user: CurrentUser = Depends(get_current_user)):
    history_list, current_sequence, error = HistoryService.get_history_list(scenario_id, user.id)
    if error: return JSONResponse({"success": False, "error": error}, status_code=400)
    undo_redo_status = HistoryService.get_undo_redo_status(scenario_id, user.id)
    return {"success": True, "history": history_list, "current_sequence": current_sequence,
            "undo_redo_status": undo_redo_status}


@api_router.get('/draft/{scenario_id}/history/status')
async def get_history_status(scenario_id: int, user: CurrentUser = Depends(get_current_user)):
    status = HistoryService.get_undo_redo_status(scenario_id, user.id)
    return {"success": True, **status}


@api_router.post('/draft/{scenario_id}/history/init')
async def init_history(scenario_id: int, user: CurrentUser = Depends(get_current_user)):
    result, error = DraftService.get_draft(scenario_id, user.id)
    if error: return JSONResponse({"success": False, "error": error}, status_code=403)
    success, hist_error = HistoryService.initialize_history(scenario_id, user.id, result['scenario'])
    if not success: return JSONResponse({"success": False, "error": hist_error}, status_code=400)
    return {"success": True, "message": "History Initialized"}


@api_router.post('/draft/{scenario_id}/history/add')
async def add_history(scenario_id: int, data: HistoryAddRequest, user: CurrentUser = Depends(get_current_user)):
    snapshot = data.snapshot
    if not snapshot:
        result, error = DraftService.get_draft(scenario_id, user.id)
        if error: return JSONResponse({"success": False, "error": error}, status_code=403)
        snapshot = result['scenario']

    success, hist_error = HistoryService.add_history(scenario_id, user.id, data.action_type, data.action_description,
                                                     snapshot)
    if not success: return JSONResponse({"success": False, "error": hist_error}, status_code=400)
    undo_redo_status = HistoryService.get_undo_redo_status(scenario_id, user.id)
    return {"success": True, "message": "History Added", "undo_redo_status": undo_redo_status}


@api_router.post('/draft/{scenario_id}/history/undo')
async def undo_history(scenario_id: int, user: CurrentUser = Depends(get_current_user)):
    restored_data, error = HistoryService.undo(scenario_id, user.id)
    if error: return JSONResponse({"success": False, "error": error}, status_code=400)
    mermaid_code = _generate_mermaid_for_response(restored_data)
    undo_redo_status = HistoryService.get_undo_redo_status(scenario_id, user.id)
    return {"success": True, "scenario": restored_data, "mermaid_code": mermaid_code,
            "undo_redo_status": undo_redo_status}


@api_router.post('/draft/{scenario_id}/history/redo')
async def redo_history(scenario_id: int, user: CurrentUser = Depends(get_current_user)):
    restored_data, error = HistoryService.redo(scenario_id, user.id)
    if error: return JSONResponse({"success": False, "error": error}, status_code=400)
    mermaid_code = _generate_mermaid_for_response(restored_data)
    undo_redo_status = HistoryService.get_undo_redo_status(scenario_id, user.id)
    return {"success": True, "scenario": restored_data, "mermaid_code": mermaid_code,
            "undo_redo_status": undo_redo_status}


@api_router.post('/draft/{scenario_id}/history/restore/{history_id}')
async def restore_history(scenario_id: int, history_id: int, user: CurrentUser = Depends(get_current_user)):
    restored_data, error = HistoryService.restore_to_point(scenario_id, user.id, history_id)
    if error: return JSONResponse({"success": False, "error": error}, status_code=400)
    mermaid_code = _generate_mermaid_for_response(restored_data)
    undo_redo_status = HistoryService.get_undo_redo_status(scenario_id, user.id)
    return {"success": True, "scenario": restored_data, "mermaid_code": mermaid_code,
            "undo_redo_status": undo_redo_status}
