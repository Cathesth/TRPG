import os
import json
import logging
import time
import threading
import glob  # <--- 이 줄을 추가해주세요!
import shutil
import uuid
from core.state import WorldState
from routes.game import save_game_session
from pathlib import Path
from passlib.context import CryptContext
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, APIRouter, Request, Depends, Form, HTTPException, Query, File, UploadFile
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
from models import get_db, Preset, CustomNPC, Scenario, ScenarioLike, User

# 변경: schemes=["bcrypt", "sha256_crypt", "pbkdf2_sha256"] -> 예전 형식도 인식 가능
pwd_context = CryptContext(
    schemes=["bcrypt", "sha256_crypt", "pbkdf2_sha256"],
    deprecated="auto"
)

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
async def mypage_view(
    request: Request,
    user: CurrentUser = Depends(get_current_user_optional),
    db: Session = Depends(get_db)
):
    # 로그인 상태라면 DB에서 최신 정보를 가져와 덮어씌움
    if user.is_authenticated:
        db_user = db.query(User).filter(User.id == user.id).first()
        if db_user:
            user = db_user  # 템플릿에 전달할 user 객체를 DB 객체로 교체

    return templates.TemplateResponse("mypage.html", {"request": request, "user": user})

# ==========================================
# [추가] 마이페이지 서브 뷰 (회원정보, 결제, 시나리오 래퍼)
# ==========================================

@api_router.get('/views/mypage/scenarios', response_class=HTMLResponse)
def get_mypage_scenarios_view():
    """마이페이지: '내 작품 보기' 클릭 시 시나리오 목록 영역 반환"""
    return """
    <div class="fade-in">
        <div class="flex items-center justify-between mb-6">
            <h2 class="text-xl font-bold text-white flex items-center gap-2">
                <i data-lucide="book-open" class="w-5 h-5 text-rpg-accent"></i> My Scenarios
            </h2>
            <div class="flex gap-2">
                <button class="px-3 py-1.5 bg-rpg-800 hover:bg-rpg-700 border border-rpg-700 rounded-lg text-xs text-white transition-colors">All</button>
                <button class="px-3 py-1.5 bg-rpg-900 hover:bg-rpg-800 border border-rpg-700 rounded-lg text-xs text-gray-400 transition-colors">Public</button>
                <button class="px-3 py-1.5 bg-rpg-900 hover:bg-rpg-800 border border-rpg-700 rounded-lg text-xs text-gray-400 transition-colors">Private</button>
            </div>
        </div>

        <div id="my-scenario-grid"
             class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6"
             hx-get="/api/scenarios?filter=my"
             hx-trigger="load"
             hx-swap="innerHTML">
            <div class="col-span-full py-12 flex flex-col items-center justify-center text-gray-500 animate-pulse">
                <i data-lucide="loader-2" class="w-8 h-8 mb-4 animate-spin"></i>
                <p>Loading your archives...</p>
            </div>
        </div>
    </div>
    <script>lucide.createIcons();</script>
    """


@api_router.get('/views/mypage/profile', response_class=HTMLResponse)
def get_profile_view(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """마이페이지: 회원 정보 수정 폼 반환"""
    if not user.is_authenticated:
        return "<div>로그인이 필요합니다.</div>"

    # DB에서 최신 유저 정보 조회 (CurrentUser에는 email/avatar_url이 없을 수 있음)
    db_user = db.query(User).filter(User.id == user.id).first()
    if not db_user:
        return "<div>회원 정보를 찾을 수 없습니다.</div>"

    username = user.id

    # [수정] user.email 대신 db_user.email을 사용해야 에러가 나지 않습니다.
    email = db_user.email or ""

    # 프로필 사진이 없으면 기본 이니셜 표시, 있으면 이미지 표시
    avatar_html = f'<span class="text-3xl font-bold text-gray-500 group-hover:text-white transition-colors">{username[:2].upper()}</span>'
    if db_user.avatar_url:
        avatar_html = f'<img src="{db_user.avatar_url}" class="w-full h-full object-cover" alt="Profile">'

    return f"""
    <div class="fade-in max-w-2xl mx-auto">
        <h2 class="text-2xl font-bold text-white mb-6 flex items-center gap-2 border-b border-rpg-700 pb-4">
            <i data-lucide="user-cog" class="w-6 h-6 text-rpg-accent"></i> Edit Profile
        </h2>

        <form onsubmit="handleProfileUpdate(event)" class="space-y-6" enctype="multipart/form-data">

            <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
                <div class="col-span-full flex flex-col items-center justify-center p-6 bg-rpg-800 rounded-xl border border-rpg-700 border-dashed hover:border-rpg-accent transition-colors cursor-pointer group"
                     onclick="document.getElementById('avatar-upload').click()">
                    <div class="w-24 h-24 rounded-full bg-rpg-900 flex items-center justify-center mb-3 relative overflow-hidden border border-rpg-700">
                        <div id="avatar-preview" class="w-full h-full flex items-center justify-center">
                            {avatar_html}
                        </div>
                        <div class="absolute inset-0 bg-black/50 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity">
                            <i data-lucide="camera" class="w-6 h-6 text-white"></i>
                        </div>
                    </div>
                    <p class="text-sm text-gray-400 group-hover:text-rpg-accent">Change Avatar</p>
                    <input type="file" id="avatar-upload" name="avatar" class="hidden" accept="image/*" onchange="previewImage(this)">
                </div>

                <div class="space-y-2">
                    <label class="text-xs font-bold text-gray-400 uppercase">Username</label>
                    <input type="text" value="{username}" disabled class="w-full bg-rpg-900/50 border border-rpg-700 rounded-lg p-3 text-gray-500 cursor-not-allowed">
                    <p class="text-[10px] text-gray-600">* 아이디는 변경할 수 없습니다.</p>
                </div>

                <div class="space-y-2">
                    <label class="text-xs font-bold text-gray-400 uppercase">Email Address</label>
                    <input type="email" name="email" value="{email}" placeholder="email@example.com" class="w-full bg-rpg-900 border border-rpg-700 rounded-lg p-3 text-white focus:border-rpg-accent focus:outline-none transition-colors">
                </div>

                <div class="space-y-2">
                    <label class="text-xs font-bold text-gray-400 uppercase">New Password</label>
                    <input type="password" name="password" placeholder="••••••••" class="w-full bg-rpg-900 border border-rpg-700 rounded-lg p-3 text-white focus:border-rpg-accent focus:outline-none transition-colors">
                </div>

                <div class="space-y-2">
                    <label class="text-xs font-bold text-gray-400 uppercase">Confirm Password</label>
                    <input type="password" name="confirm_password" placeholder="••••••••" class="w-full bg-rpg-900 border border-rpg-700 rounded-lg p-3 text-white focus:border-rpg-accent focus:outline-none transition-colors">
                </div>
            </div>

            <div class="flex justify-end gap-3 pt-6 border-t border-rpg-700">
                <button type="button" class="px-6 py-2.5 rounded-lg border border-rpg-700 text-gray-400 hover:text-white hover:bg-rpg-800 transition-colors">Cancel</button>
                <button type="submit" class="px-6 py-2.5 rounded-lg bg-rpg-accent text-black font-bold hover:bg-white transition-colors shadow-lg shadow-rpg-accent/20">Save Changes</button>
            </div>
        </form>
    </div>
    <script>lucide.createIcons();</script>
    """


# [3. 프로필 업데이트 API 추가]
@api_router.post('/auth/profile/update')
async def update_profile(
        email: str = Form(None),
        password: str = Form(None),
        confirm_password: str = Form(None),
        avatar: UploadFile = File(None),
        user: CurrentUser = Depends(get_current_user),
        db: Session = Depends(get_db)
):
    if not user.is_authenticated:
        return JSONResponse({"success": False, "error": "로그인이 필요합니다."}, status_code=401)

    # DB에서 실제 유저 객체 조회
    db_user = db.query(User).filter(User.id == user.id).first()
    if not db_user:
        return JSONResponse({"success": False, "error": "사용자를 찾을 수 없습니다."}, status_code=404)

    # 1. 비밀번호 변경 (값이 있고, 빈 문자열이 아닐 때만 실행)
    if password and password.strip():
        if len(password) > 72:
            return JSONResponse({"success": False, "error": "비밀번호는 72자 이내여야 합니다."}, status_code=400)

        if password != confirm_password:
            return JSONResponse({"success": False, "error": "비밀번호가 일치하지 않습니다."}, status_code=400)

        try:
            db_user.password_hash = pwd_context.hash(password)
        except Exception as e:
            return JSONResponse({"success": False, "error": f"비밀번호 처리 중 오류: {str(e)}"}, status_code=500)

    # 2. 이메일 업데이트
    if email is not None:
        db_user.email = email

    # 3. 프로필 사진 업로드 처리
    if avatar and avatar.filename:
        try:
            file_ext = Path(avatar.filename).suffix
            new_filename = f"{user.id}_{uuid.uuid4()}{file_ext}"
            save_path = f"static/avatars/{new_filename}"

            with open(save_path, "wb") as buffer:
                shutil.copyfileobj(avatar.file, buffer)

            db_user.avatar_url = f"/{save_path}"
        except Exception as e:
            return JSONResponse({"success": False, "error": f"이미지 업로드 실패: {str(e)}"}, status_code=500)

    try:
        db.commit()
        db.refresh(db_user)
        return {"success": True, "message": "회원 정보가 수정되었습니다."}
    except Exception as e:
        db.rollback()
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@api_router.get('/views/mypage/billing', response_class=HTMLResponse)
def get_billing_view():
    """마이페이지: 결제/플랜 변경 화면 반환"""
    return """
    <div class="fade-in">
        <h2 class="text-2xl font-bold text-white mb-2 flex items-center gap-2">
            <i data-lucide="credit-card" class="w-6 h-6 text-rpg-accent"></i> Plans & Billing
        </h2>
        <p class="text-gray-400 mb-8">모험의 규모에 맞는 플랜을 선택하세요.</p>

        <div class="grid grid-cols-1 md:grid-cols-3 gap-6">
            <div class="bg-rpg-800 border border-rpg-700 rounded-2xl p-6 flex flex-col relative overflow-hidden">
                <div class="mb-4">
                    <h3 class="text-xl font-bold text-white">Adventurer</h3>
                    <p class="text-sm text-gray-400">입문자를 위한 기본 플랜</p>
                </div>
                <div class="text-3xl font-black text-white mb-6">Free</div>
                <ul class="space-y-3 mb-8 flex-1 text-sm text-gray-300">
                    <li class="flex items-center gap-2"><i data-lucide="check" class="w-4 h-4 text-green-500"></i> 시나리오 생성 3개</li>
                    <li class="flex items-center gap-2"><i data-lucide="check" class="w-4 h-4 text-green-500"></i> 기본 AI 모델 사용</li>
                    <li class="flex items-center gap-2"><i data-lucide="check" class="w-4 h-4 text-green-500"></i> 커뮤니티 접근</li>
                </ul>
                <button class="w-full py-3 bg-rpg-700 text-gray-300 font-bold rounded-xl cursor-not-allowed">Current Plan</button>
            </div>

            <div class="bg-rpg-800 border border-rpg-accent rounded-2xl p-6 flex flex-col relative overflow-hidden shadow-[0_0_30px_rgba(56,189,248,0.15)] transform md:-translate-y-4">
                <div class="absolute top-0 right-0 bg-rpg-accent text-black text-[10px] font-bold px-3 py-1 rounded-bl-xl">POPULAR</div>
                <div class="mb-4">
                    <h3 class="text-xl font-bold text-rpg-accent">Dungeon Master</h3>
                    <p class="text-sm text-gray-400">진지한 모험가를 위한 플랜</p>
                </div>
                <div class="text-3xl font-black text-white mb-6">₩9,900 <span class="text-sm text-gray-500 font-normal">/mo</span></div>
                <ul class="space-y-3 mb-8 flex-1 text-sm text-gray-300">
                    <li class="flex items-center gap-2"><i data-lucide="check" class="w-4 h-4 text-rpg-accent"></i> 시나리오 무제한</li>
                    <li class="flex items-center gap-2"><i data-lucide="check" class="w-4 h-4 text-rpg-accent"></i> 고급 AI (GPT-4 등)</li>
                    <li class="flex items-center gap-2"><i data-lucide="check" class="w-4 h-4 text-rpg-accent"></i> 이미지 생성 50회/월</li>
                    <li class="flex items-center gap-2"><i data-lucide="check" class="w-4 h-4 text-rpg-accent"></i> 비공개 시나리오</li>
                </ul>
                <button onclick="alert('결제 모듈 연동 준비 중입니다.')" class="w-full py-3 bg-rpg-accent hover:bg-white text-black font-bold rounded-xl transition-all shadow-lg shadow-rpg-accent/20">Upgrade Now</button>
            </div>

            <div class="bg-rpg-800 border border-rpg-700 rounded-2xl p-6 flex flex-col relative overflow-hidden">
                <div class="mb-4">
                    <h3 class="text-xl font-bold text-purple-400">World Creator</h3>
                    <p class="text-sm text-gray-400">전문가를 위한 궁극의 도구</p>
                </div>
                <div class="text-3xl font-black text-white mb-6">₩29,900 <span class="text-sm text-gray-500 font-normal">/mo</span></div>
                <ul class="space-y-3 mb-8 flex-1 text-sm text-gray-300">
                    <li class="flex items-center gap-2"><i data-lucide="check" class="w-4 h-4 text-purple-400"></i> 모든 Pro 기능 포함</li>
                    <li class="flex items-center gap-2"><i data-lucide="check" class="w-4 h-4 text-purple-400"></i> 전용 파인튜닝 모델</li>
                    <li class="flex items-center gap-2"><i data-lucide="check" class="w-4 h-4 text-purple-400"></i> API 액세스</li>
                    <li class="flex items-center gap-2"><i data-lucide="check" class="w-4 h-4 text-purple-400"></i> 우선 기술 지원</li>
                </ul>
                <button onclick="alert('문의가 필요합니다.')" class="w-full py-3 bg-rpg-700 hover:bg-purple-600 hover:text-white text-white font-bold rounded-xl transition-all">Contact Sales</button>
            </div>
        </div>
    </div>
    <script>lucide.createIcons();</script>
    """


# ==========================================
# [API 라우트] 인증 (Auth) - 직접 구현으로 변경
# ==========================================
# [수정] routes/api.py -> register 함수 교체
@api_router.post('/auth/register')
async def register(data: AuthRequest, db: Session = Depends(get_db)):
    if not data.username or not data.password:
        return JSONResponse({"success": False, "error": "입력값 부족"}, status_code=400)

    # 1. 중복 아이디 확인
    existing_user = db.query(User).filter(User.id == data.username).first()

    if existing_user:
        # [추가 로직] 기존 계정의 비밀번호 데이터가 손상된 경우, 재가입을 통해 계정 복구 허용
        try:
            # 저장된 해시값이 정상적인지 확인
            pwd_context.identify(existing_user.password_hash)

            # 정상이면 -> "이미 존재하는 아이디" 에러 리턴 (기존 로직)
            return JSONResponse({"success": False, "error": "이미 존재하는 아이디"}, status_code=400)

        except (ValueError, TypeError):
            # 해시가 깨져있거나 식별 불가능한 경우 -> 비밀번호 덮어쓰기 (계정 복구)
            logger.warning(f"⚠️ Corrupted hash found for user '{data.username}'. Overwriting with new password.")

            existing_user.password_hash = pwd_context.hash(data.password)
            if data.email:
                existing_user.email = data.email

            db.commit()
            return {"success": True, "message": "손상된 계정이 복구되었습니다. 다시 로그인해주세요."}

    # 2. 신규 회원가입 (기존 로직 유지)
    try:
        hashed_password = pwd_context.hash(data.password)
        new_user = User(
            id=data.username,
            password_hash=hashed_password,
            email=data.email
        )
        db.add(new_user)
        db.commit()
        return {"success": True}
    except Exception as e:
        db.rollback()
        logger.error(f"Register Error: {e}")
        return JSONResponse({"success": False, "error": "회원가입 처리 중 오류가 발생했습니다."}, status_code=500)


@api_router.post('/auth/login')
async def login(request: Request, data: AuthRequest, db: Session = Depends(get_db)):
    if not data.username or not data.password:
        return JSONResponse({"success": False, "error": "입력값 부족"}, status_code=400)

    # 1. 사용자 조회 (UserService 대신 직접 DB 조회)
    user = db.query(User).filter(User.id == data.username).first()

    if not user or not user.password_hash:
        return JSONResponse({"success": False, "error": "아이디 또는 비밀번호가 잘못되었습니다."}, status_code=401)

    # 2. 비밀번호 검증 (직접 검증하여 'Invalid hash method' 에러 방지)
    try:
        if not pwd_context.verify(data.password, user.password_hash):
            return JSONResponse({"success": False, "error": "아이디 또는 비밀번호가 잘못되었습니다."}, status_code=401)
    except Exception as e:
        logger.error(f"Login Verify Error: {e}")
        # 해시값이 깨져있거나 비어있는 경우 로그인 실패 처리
        return JSONResponse({"success": False, "error": "아이디 또는 비밀번호가 잘못되었습니다."}, status_code=401)

    # 3. 세션 로그인 처리
    login_user(request, user)
    return {"success": True}


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
        search: Optional[str] = Query(None),
        user: CurrentUser = Depends(get_current_user_optional),
        db: Session = Depends(get_db)
):
    """
    DB에서 시나리오를 조회하여 HTML 카드로 반환합니다.
    - 메인화면: 기존 디자인 유지 (w-96, h-[26rem])
    - 마이페이지: 잘림 방지 패치 (flex-1, 이미지 비율 조정)
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
    elif filter == 'liked':  # [추가] 찜한 목록 필터
        if not user.is_authenticated:
            return HTMLResponse('<div class="col-span-full text-center text-gray-500 py-10 w-full">로그인이 필요합니다.</div>')
        # ScenarioLike 테이블과 조인하여 내가 찜한 것만 가져옴
        query = query.join(ScenarioLike, Scenario.id == ScenarioLike.scenario_id) \
            .filter(ScenarioLike.user_id == user.id)

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

    # =========================================================================
    # [추가] 검색 로직 시작
    # DB에서 가져온 목록을 파이썬 레벨에서 검색어로 필터링합니다.
    # =========================================================================
    if search:
        search_term = search.lower().strip()
        filtered_scenarios = []
        for s in scenarios:
            # 데이터 파싱 (검색 대상을 확인하기 위해 미리 추출)
            s_data = s.data if isinstance(s.data, dict) else {}
            if 'scenario' in s_data: s_data = s_data['scenario']

            title = s.title or ""
            # 설명 데이터 추출 (prologue 또는 desc)
            desc = s_data.get('prologue', s_data.get('desc', ''))

            # 제목이나 설명에 검색어가 포함되어 있는지 확인
            if search_term in title.lower() or search_term in desc.lower():
                filtered_scenarios.append(s)

        # 필터링된 결과로 교체
        scenarios = filtered_scenarios

    if not scenarios:
        if filter == 'liked': msg = "찜한 시나리오가 없습니다."
        elif search: msg = "검색 결과가 없습니다."
        elif filter == 'my': msg = "아직 생성한 시나리오가 없습니다."
        else: msg = "등록된 시나리오가 없습니다."
        return HTMLResponse(f'<div class="col-span-full text-center text-gray-500 py-12 w-full flex flex-col items-center"><i data-lucide="inbox" class="w-10 h-10 mb-2 opacity-50"></i><p>{msg}</p></div>')

    # 5. HTML 생성
    from datetime import datetime
    import time as time_module
    current_ts = time_module.time()
    NEW_THRESHOLD = 30 * 60

    # [추가] 현재 유저가 찜한 시나리오 ID 목록 미리 조회 (성능 최적화)
    liked_scenario_ids = set()
    if user.is_authenticated:
        likes = db.query(ScenarioLike.scenario_id).filter(ScenarioLike.user_id == user.id).all()
        liked_scenario_ids = {l[0] for l in likes}

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

        # [디자인 분기 설정]
        if filter == 'my':
            # [마이페이지 수정]
            # 1. w-full aspect-square: 그리드에 맞춤
            # 2. h-[45%]: 이미지 높이를 줄여 텍스트 공간 확보 (기존 55%)
            # 3. p-4: 패딩을 살짝 줄여 내부 공간 확보 (기존 p-5)
            card_style = "w-full aspect-square"
            img_height = "h-[45%]"
            content_padding = "p-4"
        else:
            # [메인화면 유지]
            # 1. w-96 h-[26rem]: 기존 크기 유지
            # 2. h-52: 이미지 높이 유지
            # 3. p-5: 패딩 유지
            card_style = "w-96 h-[26rem] flex-shrink-0 snap-center"
            img_height = "h-52"
            content_padding = "p-5"

        # [추가] 하트 아이콘 상태 결정
        is_liked = s.id in liked_scenario_ids
        # 찜 상태면 빨간색 채움(fill-red-500), 아니면 흰색 테두리(text-white/70)
        heart_class = "fill-red-500 text-red-500" if is_liked else "text-white/70 hover:text-red-500"

        # [추가] 하트 버튼 HTML (이미지 우측 상단에 배치)
        like_btn = f"""
        <button onclick="toggleLike({s.id}, this); event.stopPropagation();" 
                class="absolute top-2 right-2 p-2 rounded-full bg-black/50 backdrop-blur-sm hover:bg-black/70 transition-all z-10 {heart_class}">
            <i data-lucide="heart" class="w-5 h-5 transition-transform active:scale-90"></i>
        </button>
        """

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
        # 핵심 수정: h-full -> flex-1 (내용물이 남은 공간만 차지하도록 변경하여 넘침 방지)
        card_html = f"""
        <div class="scenario-card-base group bg-[#0f172a] border border-[#1e293b] rounded-xl overflow-hidden hover:border-[#38bdf8] transition-all flex flex-col shadow-lg relative {card_style}">
            <div class="relative {img_height} overflow-hidden bg-black shrink-0">
                <img src="{img_src}" class="w-full h-full object-cover transition-transform duration-500 group-hover:scale-110 opacity-80 group-hover:opacity-100">
                
                {like_btn}
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
                    <p class="text-sm text-gray-400 line-clamp-2 leading-relaxed min-h-[3em]">{desc}</p>
                </div>

                {buttons_html}
            </div>
        </div>
        """
        html += card_html

    html += '<script>lucide.createIcons();</script>'
    return HTMLResponse(content=html)


# =========================================================================
# 찜목록 함수
# =========================================================================
@api_router.post('/scenarios/{scenario_id}/like')
def toggle_like(
        scenario_id: int,
        user: CurrentUser = Depends(get_current_user),
        db: Session = Depends(get_db)
):
    if not user.is_authenticated:
        return JSONResponse({"success": False, "error": "로그인이 필요합니다."}, status_code=401)

    # 이미 찜했는지 확인
    existing_like = db.query(ScenarioLike).filter(
        ScenarioLike.user_id == user.id,
        ScenarioLike.scenario_id == scenario_id
    ).first()

    if existing_like:
        db.delete(existing_like)  # 이미 있으면 삭제 (찜 취소)
        liked = False
    else:
        new_like = ScenarioLike(user_id=user.id, scenario_id=scenario_id)
        db.add(new_like)  # 없으면 추가 (찜 하기)
        liked = True

    db.commit()
    return {"success": True, "liked": liked}

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
