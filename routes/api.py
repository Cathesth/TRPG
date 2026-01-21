import os
import json
import logging
import time
import threading
import glob
import shutil
import uuid
from core.state import WorldState
from routes.game import save_game_session
from pathlib import Path
from passlib.context import CryptContext
# [추가] 11번 계정(scrypt) 지원을 위한 라이브러리
from werkzeug.security import check_password_hash
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, APIRouter, Request, Depends, Form, HTTPException, Query, File, UploadFile
from fastapi.responses import JSONResponse, HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from starlette.concurrency import run_in_threadpool

# 빌더 에이전트 및 코어 유틸리티
from builder_agent import generate_scenario_from_graph, set_progress_callback, generate_single_npc, generate_scene_content
from core.state import GameState
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
from services.image_service import get_image_service
from services.preset_service import PresetService  # 누락된 임포트 추가

# 인증 및 모델
from routes.auth import get_current_user, get_current_user_optional, login_user, logout_user, CurrentUser
from models import get_db, Preset, CustomNPC, Scenario, ScenarioLike, User

# [api.py 상단 임포트 추가]
from authlib.integrations.starlette_client import OAuth
from starlette.config import Config
from starlette.middleware.sessions import SessionMiddleware

print("=========================================")
print(f"👉 DEBUG: KAKAO_CLIENT_ID = [{os.getenv('KAKAO_CLIENT_ID')}]")
print(f"👉 DEBUG: KAKAO_CLIENT_SECRET = [{os.getenv('KAKAO_CLIENT_SECRET')}]")
print("=========================================")

# [👇 추가할 코드] 변수가 없으면 서버를 켜지 말고 에러를 띄워라! (확인용)
if not os.getenv('KAKAO_CLIENT_ID'):
    # 로컬 개발 환경 등에서 환경변수가 없을 때를 대비해 경고만 출력하고 넘어갈 수도 있음
    logger.warning("🚨 [WARNING] KAKAO_CLIENT_ID 환경 변수가 없습니다! 소셜 로그인이 작동하지 않을 수 있습니다.")
    # raise RuntimeError("🚨 [CRITICAL ERROR] KAKAO_CLIENT_ID 환경 변수가 없습니다! Railway 변수 설정을 확인하세요.")

# .env 파일을 읽기 위한 설정
config = Config('.env')
oauth = OAuth(config)

# 1. Google 등록
oauth.register(
    name='google',
    client_id=os.getenv('GOOGLE_CLIENT_ID'),
    client_secret=os.getenv('GOOGLE_CLIENT_SECRET'),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

oauth.register(
    name='naver',
    client_id=os.getenv('NAVER_CLIENT_ID'),
    client_secret=os.getenv('NAVER_CLIENT_SECRET'),
    api_base_url='https://openapi.naver.com/v1/nid/me',
    access_token_url='https://nid.naver.com/oauth2.0/token',
    authorize_url='https://nid.naver.com/oauth2.0/authorize',
    client_kwargs={'scope': 'profile'}
)

oauth.register(
    name='kakao',
    client_id=os.getenv('KAKAO_CLIENT_ID'),
    client_secret=os.getenv('KAKAO_CLIENT_SECRET'),
    api_base_url='https://kapi.kakao.com/v2/user/me',
    access_token_url='https://kauth.kakao.com/oauth/token',
    authorize_url='https://kauth.kakao.com/oauth/authorize',
    client_kwargs={
        'scope': 'account_email profile_nickname',
        # [핵심 해결책] ID와 비밀번호를 Body에 담아서 보내라는 설정입니다.
        'token_endpoint_auth_method': 'client_secret_post',
    }
)

# 변경: schemes=["bcrypt", "sha256_crypt", "pbkdf2_sha256"] -> 예전 형식도 인식 가능
pwd_context = CryptContext(
    schemes=["bcrypt"],
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


class ImageGenerateRequest(BaseModel):
    image_type: str  # 'npc', 'enemy', 'background'
    description: str
    scenario_id: Optional[int] = None
    target_id: Optional[str] = None


# [추가] 빌더에서 그래프 데이터(Nodes/Edges)를 직접 보내 검수 요청할 때 사용하는 모델
class BuilderAuditRequest(BaseModel):
    scenario: Dict[str, Any]
    scene_id: Optional[str] = None  # None이면 전체 검수
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

            # [추가] 사용자의 시나리오 통계 조회
            stats = ScenarioService.get_user_statistics(user.id)

    # [수정] stats 데이터를 템플릿 context에 포함하여 전달
    return templates.TemplateResponse("mypage.html", {"request": request, "user": user, "stats": stats})


# [추가] 메인화면 헤더 프로필 로드용 (HTMX)
@api_router.get('/views/header-profile', response_class=HTMLResponse)
def header_profile_view(
        request: Request,
        user: CurrentUser = Depends(get_current_user_optional),
        db: Session = Depends(get_db)
):
    """메인 헤더 우측 상단 프로필/로그인 버튼 영역을 렌더링"""

    # 1. 로그인 상태: DB에서 최신 정보 조회 후 프로필 표시
    if user.is_authenticated:
        db_user = db.query(User).filter(User.id == user.id).first()
        avatar_url = db_user.avatar_url if db_user else None

        if avatar_url:
            inner_html = f'<img src="{avatar_url}" class="w-full h-full object-cover">'
        else:
            inner_html = '<i data-lucide="user" class="w-6 h-6"></i>'

        return f"""
        <div class="flex items-center gap-3 cursor-pointer group" onclick="location.href='/views/mypage'" title="마이페이지">
            <button class="text-gray-400 group-hover:text-white transition-colors p-0.5 rounded-full bg-rpg-800 border border-rpg-700 group-hover:border-rpg-accent shadow-md overflow-hidden w-10 h-10 flex items-center justify-center">
                {inner_html}
            </button>
        </div>
        <script>lucide.createIcons();</script>
        """

    # 2. 비로그인 상태: 로그인 버튼 표시
    else:
        return """
        <button onclick="openModal('login-modal')" class="flex items-center gap-2 px-5 py-2.5 bg-rpg-accent hover:bg-white text-black font-bold rounded shadow-lg shadow-rpg-accent/20 transition-all">
            <i data-lucide="log-in" class="w-4 h-4"></i> LOGIN
        </button>
        <script>lucide.createIcons();</script>
        """


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

            <div class="flex gap-2" id="filter-buttons">
                <button hx-get="/api/scenarios?filter=my&visibility=all" 
                        hx-target="#my-scenario-grid"
                        onclick="updateFilterStyle(this)"
                        class="px-3 py-1.5 bg-rpg-800 hover:bg-rpg-700 border border-rpg-700 rounded-lg text-xs text-white transition-colors">All</button>

                <button hx-get="/api/scenarios?filter=my&visibility=public" 
                        hx-target="#my-scenario-grid"
                        onclick="updateFilterStyle(this)"
                        class="px-3 py-1.5 bg-rpg-900 hover:bg-rpg-800 border border-rpg-700 rounded-lg text-xs text-gray-400 transition-colors">Public</button>

                <button hx-get="/api/scenarios?filter=my&visibility=private" 
                        hx-target="#my-scenario-grid"
                        onclick="updateFilterStyle(this)"
                        class="px-3 py-1.5 bg-rpg-900 hover:bg-rpg-800 border border-rpg-700 rounded-lg text-xs text-gray-400 transition-colors">Private</button>
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

    # 1. 비밀번호 변경 (기존 로직 유지)
    if password and password.strip():
        if len(password) > 72:
            return JSONResponse({"success": False, "error": "비밀번호는 72자 이내여야 합니다."}, status_code=400)
        if password != confirm_password:
            return JSONResponse({"success": False, "error": "비밀번호가 일치하지 않습니다."}, status_code=400)
        try:
            db_user.password_hash = pwd_context.hash(password)
        except Exception as e:
            return JSONResponse({"success": False, "error": f"비밀번호 처리 중 오류: {str(e)}"}, status_code=500)

    # 2. 이메일 업데이트 (기존 로직 유지)
    if email is not None:
        db_user.email = email

    # 3. 프로필 사진 업로드 처리 (S3 저장 방식으로 변경)
    if avatar and avatar.filename:
        try:
            # [수정] 로컬 파일 저장이 아닌 S3 업로드로 변경하여 배포 후에도 이미지 유지
            from core.s3_client import get_s3_client  # 필요한 시점에 임포트

            s3 = get_s3_client()
            # S3 세션이 초기화되지 않았을 경우 안전장치
            if not s3._session:
                await s3.initialize()

            file_ext = Path(avatar.filename).suffix
            new_filename = f"{user.id}_{uuid.uuid4()}{file_ext}"

            # S3 버킷 내 저장 경로 (static/avatars 대신 avatars/ 폴더 사용 권장)
            s3_key = f"avatars/{new_filename}"

            # 업로드할 파일 내용 읽기
            content = await avatar.read()

            # S3에 파일 업로드
            async with s3._session.client(
                    's3',
                    endpoint_url=s3.endpoint,
                    region_name=s3.region,
                    use_ssl=s3.use_ssl
            ) as client:
                await client.put_object(
                    Bucket=s3.bucket,
                    Key=s3_key,
                    Body=content,
                    ContentType=avatar.content_type or 'image/png'
                )

            # [중요] DB에는 프록시 URL 저장
            # app.py에 있는 '/image/serve/{path}' 라우트가 S3 이미지를 대신 가져와 보여줍니다.
            db_user.avatar_url = f"/image/serve/{s3_key}"

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
         <div class="bg-rpg-800 border border-rpg-700 rounded-2xl p-6 text-center text-gray-400">
            플랜 정보를 로드하는 중...

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
@api_router.post('/auth/register')
async def register(data: AuthRequest, db: Session = Depends(get_db)):
    if not data.username or not data.password:
        return JSONResponse({"success": False, "error": "입력값 부족"}, status_code=400)

    # 1. 중복 아이디 확인
    existing_user = db.query(User).filter(User.id == data.username).first()

    if existing_user:
        return JSONResponse({"success": False, "error": "이미 존재하는 아이디"}, status_code=400)

    # 2. 신규 회원가입 처리
    try:
        # 비밀번호 해싱 (설정된 암호화 방식 사용)
        hashed_password = pwd_context.hash(data.password)

        # [수정] UserService.create_user 내부에서 초기 토큰 지급을 처리하지만,
        # 여기서는 User 모델을 직접 사용하므로 수동으로 설정 필요할 수 있음.
        # 하지만 UserService.create_user와 일관성을 위해 모델 기본값(1000)을 믿거나 명시적으로 설정.
        # models.py에서 default=1000이므로 별도 설정 불필요.
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

    # 2. 비밀번호 검증 (이중 체크: Passlib -> Werkzeug)
    verified = False

    # (A) Passlib 시도 (bcrypt 등 표준 해시)
    try:
        if pwd_context.verify(data.password, user.password_hash):
            verified = True
    except (ValueError, TypeError):
        # Passlib이 식별 못한 경우 (예: unknown hash format)
        pass

    # (B) Passlib 실패 시, Werkzeug 시도 (11번 계정 scrypt 해시)
    if not verified:
        try:
            # werkzeug의 scrypt 형식을 직접 검증
            if check_password_hash(user.password_hash, data.password):
                verified = True
        except Exception as e:
            logger.error(f"Werkzeug check failed: {e}")
            pass

    if not verified:
        logger.warning(f"Login failed for user: {data.username}")
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


# [추가] 유저 잔액 조회 API
@api_router.get('/user/status')
async def get_user_status(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    if not user.is_authenticated:
        return JSONResponse({"success": False, "error": "Login required"}, status_code=401)

    db_user = db.query(User).filter(User.id == user.id).first()
    if not db_user:
         return JSONResponse({"success": False, "error": "User not found"}, status_code=404)

    return {
        "success": True,
        "username": db_user.id,
        "balance": db_user.token_balance,
        "tutorial_completed": getattr(db_user, 'tutorial_completed', False),
        "avatar_url": getattr(db_user, 'avatar_url', None)
    }


@api_router.post('/user/tutorial/complete')
async def complete_tutorial(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    if not user.is_authenticated:
        return JSONResponse({"success": False, "error": "Login required"}, status_code=401)

    db_user = db.query(User).filter(User.id == user.id).first()
    if db_user:
        if not getattr(db_user, 'tutorial_completed', False):
            db_user.tutorial_completed = True
            db.commit()
            logger.info(f"User {user.id} completed tutorial.")
        return {"success": True, "message": "Tutorial completed"}
    
    return JSONResponse({"success": False, "error": "User not found"}, status_code=404)


# ---------------------------------------------------------
# [추가] 소셜 로그인 라우트
# ---------------------------------------------------------

@api_router.get('/auth/login/{provider}')
async def login_social(provider: str, request: Request):
    """
    프론트엔드에서 '구글 로그인' 버튼 누르면 이 주소로 이동
    예: <a href="/api/auth/login/google">Google Login</a>
    """
    # [수정 후 코드] URL 객체를 문자열로 먼저 변환해야 합니다.
    redirect_uri = str(request.url_for('auth_callback', provider=provider))

    # 간혹 https/http 프로토콜 문제 발생 시 강제 변환 (배포 환경 고려)
    if "localhost" not in redirect_uri:
        redirect_uri = redirect_uri.replace("http://", "https://")

    return await oauth.create_client(provider).authorize_redirect(request, redirect_uri)


@api_router.get('/auth/callback/{provider}', name="auth_callback")
async def auth_callback(provider: str, request: Request, db: Session = Depends(get_db)):
    """
    소셜 로그인 성공 후 돌아오는 콜백 주소
    """
    try:
        client = oauth.create_client(provider)
        token = await client.authorize_access_token(request)
    except Exception as e:
        logger.error(f"OAuth Token Error: {e}")
        return JSONResponse({"success": False, "error": "소셜 인증 실패"}, status_code=400)

    # 사용자 정보 가져오기
    user_info = None
    social_id = None
    email = None
    nickname = None

    if provider == 'google':
        user_info = token.get('userinfo')
        if not user_info:
            user_info = await client.userinfo(token=token)
        email = user_info.get('email')
        social_id = user_info.get('sub')  # 구글 고유 ID
        nickname = user_info.get('name')

    elif provider == 'naver':
        resp = await client.get('https://openapi.naver.com/v1/nid/me', token=token)
        profile = resp.json().get('response', {})
        email = profile.get('email')
        social_id = profile.get('id')
        nickname = profile.get('name') or profile.get('nickname')

    elif provider == 'kakao':
        resp = await client.get('https://kapi.kakao.com/v2/user/me', token=token)
        profile = resp.json()
        kakao_account = profile.get('kakao_account', {})

        social_id = str(profile.get('id'))
        email = kakao_account.get('email')
        nickname = kakao_account.get('profile', {}).get('nickname')

    if not email:
        return JSONResponse({"success": False, "error": "이메일 정보를 가져올 수 없습니다."}, status_code=400)

    # ---------------------------------------------------------
    # [핵심 로직] DB 연동 (기존 회원 확인 또는 자동 가입)
    # ---------------------------------------------------------

    # 1. 이메일로 기존 유저 확인
    existing_user = db.query(User).filter(User.email == email).first()

    if existing_user:
        # 이미 가입된 이메일이면 바로 로그인 처리
        login_user(request, existing_user)
        return RedirectResponse(url="/")  # 메인 페이지로 이동

    # 2. 가입된 유저가 없으면 '자동 회원가입' 진행
    # 소셜 유저는 비밀번호가 없으므로 랜덤 생성하거나 비워둠
    import uuid

    # ID 충돌 방지를 위해 이메일을 ID로 쓰거나, 소셜 전용 prefix 붙임
    # 예: google_12345
    new_user_id = f"{provider}_{social_id[:8]}"

    # 혹시나 ID가 중복되면 이메일 앞부분 사용 등 로직 추가 필요
    if db.query(User).filter(User.id == new_user_id).first():
        new_user_id = email.split('@')[0] + f"_{str(uuid.uuid4())[:4]}"

    random_password = str(uuid.uuid4())
    hashed_password = pwd_context.hash(random_password)

    new_user = User(
        id=new_user_id,
        password_hash=hashed_password,
        email=email,
        # [수정] models.py에서 default=1000 토큰이 자동 할당됨
    )

    try:
        db.add(new_user)
        db.commit()
        db.refresh(new_user)

        # 가입 후 로그인 처리
        login_user(request, new_user)
        return RedirectResponse(url="/")

    except Exception as e:
        db.rollback()
        logger.error(f"Social Register Error: {e}")
        return JSONResponse({"success": False, "error": "소셜 가입 중 오류 발생"}, status_code=500)


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


# [1. 헬퍼 함수 추가] 잠금 버튼 HTML 생성기
def _generate_lock_button(scenario_id: int, is_public: bool):
    """
    HTMX로 작동하는 잠금/해제 버튼 HTML을 반환합니다.
    - 위치: 이미지 상단 우측 (하트 버튼 왼쪽: right-14)
    - 스타일: 원형 반투명 버튼
    """
    # 하트 버튼과 동일한 스타일 + 위치만 왼쪽(right-14)으로 배치
    base_style = "absolute top-2 right-14 p-2 rounded-full bg-black/50 backdrop-blur-sm hover:bg-black/70 transition-all z-20 flex items-center justify-center"

    if is_public:
        # [현재: 공개 상태] -> 파란색 열린 자물쇠 아이콘 (누르면 -> 비공개로 전환)
        return f"""
            <button hx-post="/api/scenarios/{scenario_id}/toggle-public" 
                    hx-swap="outerHTML"
                    class="{base_style} text-blue-400 hover:text-blue-300" 
                    title="현재 공개됨 (클릭하여 비공개 전환)">
                <i data-lucide="lock-open" class="w-5 h-5"></i>
                <script>lucide.createIcons();</script>
            </button>
            """
    else:
        # [현재: 비공개 상태] -> 빨간색 잠긴 자물쇠 아이콘 (누르면 -> 공개로 전환)
        return f"""
            <button hx-post="/api/scenarios/{scenario_id}/toggle-public" 
                    hx-swap="outerHTML"
                    class="{base_style} text-red-500 hover:text-red-400" 
                    title="현재 비공개 (클릭하여 공개 전환)">
                <i data-lucide="lock" class="w-5 h-5"></i>
                <script>lucide.createIcons();</script>
            </button>
            """


# [2. API 엔드포인트 수정] 토글 요청 처리 + 통계 숫자 실시간 업데이트
@api_router.post('/scenarios/{scenario_id}/toggle-public')
async def toggle_scenario_public(scenario_id: int, user: CurrentUser = Depends(get_current_user)):
    if not user.is_authenticated:
        return HTMLResponse("Login required", status_code=401)

    success, msg, new_state = ScenarioService.toggle_public(scenario_id, user.id)

    if not success:
        return HTMLResponse(f"<script>alert('{msg}');</script>", status_code=400)

    # 1. 바뀐 상태에 맞는 새로운 버튼 HTML 생성
    button_html = _generate_lock_button(scenario_id, new_state)

    # 2. [추가됨] 최신 통계(Private 개수) 다시 계산
    stats = ScenarioService.get_user_statistics(user.id)

    # 3. [추가됨] 통계 숫자 업데이트용 HTML (OOB Swap)
    # id="stat-private-count"인 태그를 찾아서 이 내용으로 바꿔치기합니다.
    stats_html = f'<span id="stat-private-count" hx-swap-oob="true" class="text-rpg-accent font-bold text-xl">{stats["private"]}</span>'

    # 4. 버튼과 통계 HTML을 합쳐서 반환
    return HTMLResponse(button_html + stats_html)


# --- [MODIFIED] 시나리오 생성 API (토큰 과금 적용) ---
class GenerateRequest(BaseModel):
    graph_data: Dict[str, Any]
    model: str = "gpt-4o-mini"


@api_router.post('/builder/generate')
async def generate_scenario(request: GenerateRequest, user: CurrentUser = Depends(get_current_user)):
    """
    빌더 그래프 데이터를 기반으로 시나리오 생성 (토큰 차감 포함)
    """
    if not user.is_authenticated:
        return JSONResponse({"success": False, "error": "Login required"}, status_code=401)

    # 잔액 확인
    balance = UserService.get_user_balance(user.id)
    if balance <= 0:
        return JSONResponse({"success": False, "error": "토큰이 부족합니다. 충전 후 이용해주세요."}, status_code=402)

    global progress_data
    progress_data = {"status": "starting", "message": "생성 작업 시작...", "percent": 0}

    try:
        # [수정] user.id를 전달하여 토큰 과금 수행
        result = await run_in_threadpool(
            generate_scenario_from_graph,
            api_key="",
            user_data=request.graph_data,
            model_name=request.model,
            user_id=user.id
        )

        progress_data = {"status": "complete", "message": "완료!", "percent": 100}

        # 남은 잔액 조회
        new_balance = UserService.get_user_balance(user.id)

        return {"success": True, "data": result, "remaining_balance": new_balance}

    except Exception as e:
        logger.error(f"Generation error: {e}")
        progress_data = {"status": "error", "message": str(e), "percent": 0}
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# --- [MODIFIED] NPC 생성 API (Builder 내 - 토큰 과금 적용) ---
class NpcGenRequest(BaseModel):
    scenario_title: str
    scenario_summary: str
    user_request: str
    model: str = "gpt-4o-mini"


@api_router.post('/builder/generate-npc')
async def generate_npc(request: NpcGenRequest, user: CurrentUser = Depends(get_current_user)):
    """
    단일 NPC 생성 (토큰 차감 포함)
    """
    if not user.is_authenticated:
        return JSONResponse({"success": False, "error": "Login required"}, status_code=401)

    try:
        # [수정] user.id 전달
        npc_data = await run_in_threadpool(
            generate_single_npc,
            scenario_title=request.scenario_title,
            scenario_summary=request.scenario_summary,
            user_request=request.user_request,
            model_name=request.model,
            user_id=user.id
        )

        if not npc_data:
            return JSONResponse({"success": False, "error": "Failed to generate NPC"}, status_code=500)

        return {"success": True, "data": npc_data}

    except Exception as e:
        logger.error(f"NPC Gen Error: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# [교체] routes/api.py -> list_scenarios 함수
@api_router.get('/scenarios', response_class=HTMLResponse)
def list_scenarios(
        request: Request,
        sort: str = Query('newest'),
        filter: str = Query('public'),
        visibility: str = Query('all'), # [추가] 공개/비공개 필터 파라미터
        limit: int = Query(10),
        search: Optional[str] = Query(None),
        user: CurrentUser = Depends(get_current_user_optional),
        db: Session = Depends(get_db)
):
    # (기존 코드 유지 - 검색 로직 등 포함된 버전)
    query = db.query(Scenario)

    if filter == 'my':
        # [내 시나리오] 로그인 필요, 내 작품만 조회 (비공개 포함)
        if not user.is_authenticated:
            return HTMLResponse('<div class="col-span-full text-center text-gray-500 py-10 w-full">로그인이 필요합니다.</div>')
        query = query.filter(Scenario.author_id == user.id)

        # [추가] 마이페이지 내 공개/비공개 필터링 로직
        if visibility == 'public':
            query = query.filter(Scenario.is_public == True)
        elif visibility == 'private':
            query = query.filter(Scenario.is_public == False)
        # visibility == 'all' 이면 필터 없이 모두 조회 (기본 동작)

    # filter='all'은 전체 조회
    elif filter == 'liked':
        # [찜한 목록] 로그인 필요, 내가 찜한 것만 조회
        if not user.is_authenticated:
            return HTMLResponse('<div class="col-span-full text-center text-gray-500 py-10 w-full">로그인이 필요합니다.</div>')
        query = query.join(ScenarioLike, Scenario.id == ScenarioLike.scenario_id) \
            .filter(ScenarioLike.user_id == user.id)

    else:
        # [수정됨] public, all(메인화면), 그 외 모든 경우 -> 무조건 '공개(True)'된 시나리오만 조회
        # 이 코드가 없어서 메인화면에 비공개 시나리오가 노출되었습니다.
        query = query.filter(Scenario.is_public == True)

    # 3. 정렬
    if sort == 'oldest':
        query = query.order_by(Scenario.created_at.asc())
    elif sort == 'name_asc':
        query = query.order_by(Scenario.title.asc())
    else:
        query = query.order_by(Scenario.created_at.desc())

    if limit:
        query = query.limit(limit)

    scenarios = query.all()

    # 검색 필터링 (Python 레벨)
    if search:
        search_term = search.lower().strip()
        filtered_scenarios = []
        for s in scenarios:
            s_data = s.data if isinstance(s.data, dict) else {}
            if 'scenario' in s_data: s_data = s_data['scenario']
            title = s.title or ""
            desc = s_data.get('prologue', s_data.get('desc', ''))
            if search_term in title.lower() or search_term in desc.lower():
                filtered_scenarios.append(s)
        scenarios = filtered_scenarios

    if not scenarios:
        if filter == 'liked':
            msg = "찜한 시나리오가 없습니다."
        elif search:
            msg = "검색 결과가 없습니다."
        elif filter == 'my':
            msg = "아직 생성한 시나리오가 없습니다."
        else:
            msg = "등록된 시나리오가 없습니다."
        return HTMLResponse(
            f'<div class="col-span-full text-center text-gray-500 py-12 w-full flex flex-col items-center"><i data-lucide="inbox" class="w-10 h-10 mb-2 opacity-50"></i><p>{msg}</p></div>')

    # HTML 생성
    from datetime import datetime
    import time as time_module
    current_ts = time_module.time()
    NEW_THRESHOLD = 30 * 60

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

        created_ts = s.created_at.timestamp() if s.created_at else 0
        time_str = s.created_at.strftime('%Y-%m-%d') if s.created_at else "-"

        img_src = s_data.get('image') or "https://images.unsplash.com/photo-1519074069444-1ba4fff66d16?q=80&w=800"

        is_new = (current_ts - created_ts) < NEW_THRESHOLD
        new_badge = '<span class="ml-2 text-[10px] bg-red-500 text-white px-1.5 py-0.5 rounded-full font-bold animate-pulse">NEW</span>' if is_new else ''

        # [수정 포인트 1] 잠금 버튼 HTML 생성 (마이페이지에서만 보임)
        lock_btn_html = ""
        # "토글버튼은 마이페이지 에만 볼 수 있게" 요청 반영 (filter == 'my' 체크)
        if is_owner and filter == 'my':
            lock_btn_html = _generate_lock_button(s.id, s.is_public)

        # [디자인 분기 설정]
        if filter == 'my':
            card_style = "w-full aspect-square"
            img_height = "h-[45%]"
            content_padding = "p-4"
        else:
            card_style = "w-96 h-[26rem] flex-shrink-0 snap-center"
            img_height = "h-52"
            content_padding = "p-5"

        is_liked = s.id in liked_scenario_ids
        heart_class = "fill-red-500 text-red-500" if is_liked else "text-white/70 hover:text-red-500"

        like_btn = f"""
        <button onclick="toggleLike({s.id}, this); event.stopPropagation();" 
                class="absolute top-2 right-2 p-2 rounded-full bg-black/50 backdrop-blur-sm hover:bg-black/70 transition-all z-10">
             <i data-lucide="heart" class="w-5 h-5 transition-transform active:scale-90 {heart_class}"></i>
        </button>
        """

        if is_owner:
            buttons_html = f"""          
            <div class="flex flex-wrap items-center gap-2 mt-auto pt-3 border-t border-white/10 shrink-0">
                <button onclick="playScenario('{fid}', this)" class="flex-1 py-2 bg-[#1e293b] hover:bg-[#38bdf8] hover:text-black text-white font-bold rounded-lg transition-all flex items-center justify-center gap-2 shadow-md border border-[#1e293b] text-xs min-w-[80px]">
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
                    <div class="mt-auto pt-3 border-t border-white/10 shrink-0">
                        <button onclick="playScenario('{fid}', this)" class="w-full py-2 bg-[#1e293b] hover:bg-[#38bdf8] hover:text-black text-white font-bold rounded-lg transition-all flex items-center justify-center gap-2 shadow-md border border-[#1e293b] text-xs">
                            <i data-lucide="play" class="w-3 h-3 fill-current"></i> PLAY NOW
                        </button>
                    </div>
                    """

        card_html = f"""
        <div class="scenario-card-base group bg-[#0f172a] border border-[#1e293b] rounded-xl overflow-hidden hover:border-[#38bdf8] transition-all flex flex-col shadow-lg relative {card_style}">
            <div class="relative {img_height} overflow-hidden bg-black shrink-0">
                <img src="{img_src}" class="w-full h-full object-cover transition-transform duration-500 group-hover:scale-110 opacity-80 group-hover:opacity-100">

                {lock_btn_html}

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


@api_router.post('/scenarios/{scenario_id}/like')
def toggle_like(
        scenario_id: int,
        user: CurrentUser = Depends(get_current_user),
        db: Session = Depends(get_db)
):
    if not user.is_authenticated:
        return JSONResponse({"success": False, "error": "로그인이 필요합니다."}, status_code=401)

    existing_like = db.query(ScenarioLike).filter(
        ScenarioLike.user_id == user.id,
        ScenarioLike.scenario_id == scenario_id
    ).first()

    if existing_like:
        db.delete(existing_like)
        liked = False
    else:
        new_like = ScenarioLike(user_id=user.id, scenario_id=scenario_id)
        db.add(new_like)
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
        user: CurrentUser = Depends(get_current_user_optional),
        db: Session = Depends(get_db)
):
    import uuid
    from core.state import WorldState

    user_id = user.id if user.is_authenticated else None
    result, error = ScenarioService.load_scenario(filename, user_id)
    if error:
        return JSONResponse({"error": error}, status_code=400)

    scenario = result['scenario']
    start_id = pick_start_scene_id(scenario)

    new_session_key = str(uuid.uuid4())
    logger.info(f"🆕 [LOAD_SCENARIO] Creating new session: {new_session_key}")

    world_state_instance = WorldState()
    world_state_instance.reset()
    world_state_instance.initialize_from_scenario(scenario)

    game_state_instance = GameState()
    game_state_instance.config['title'] = scenario.get('title', 'Loaded')

    scenario_id = scenario.get('id', 0)

    player_state = {
        "scenario_id": scenario_id,
        "current_scene_id": "prologue",
        "start_scene_id": start_id,
        "player_vars": result['player_vars'],
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
        "_internal_flags": {},
        "stuck_count": 0,
        "world_state": world_state_instance.to_dict()
    }

    game_state_data = {
        "config": game_state_instance.config,
        "state": player_state
    }

    from routes.game import save_game_session

    try:
        saved_key = save_game_session(db, player_state, user_id=user_id, session_key=new_session_key)
        logger.info(f"✅ [LOAD_SCENARIO] Session persisted to DB: {saved_key} (scenario_id={scenario_id})")
    except Exception as e:
        logger.error(f"❌ [LOAD_SCENARIO] Failed to save session to DB: {e}")
        saved_key = new_session_key

    return {
        "success": True,
        "session_key": saved_key,
        "scenario_id": scenario_id,
        "game_state": game_state_data,
        "player_vars": result['player_vars'],
        "start_scene_id": start_id
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
    import uuid
    from core.state import WorldState, GameState

    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return JSONResponse({"error": "API Key 없음"}, status_code=400)

    react_flow_data = await request.json()
    selected_model = react_flow_data.get('model', 'openai/tngtech/deepseek-r1t2-chimera:free')

    update_build_progress(status="building", step="0/5", detail="준비 중...", progress=0)

    try:
        set_progress_callback(update_build_progress)

        # [수정] user.id 전달하여 토큰 과금
        user_id = user.id if user.is_authenticated else None

        scenario_json = await run_in_threadpool(
            generate_scenario_from_graph,
            api_key,
            react_flow_data,
            model_name=selected_model,
            user_id=user_id  # 추가
        )

        fid, error = ScenarioService.save_scenario(scenario_json, user_id=user_id)

        if error:
            update_build_progress(status="error", detail=f"저장 오류: {error}")
            return JSONResponse({"error": error}, status_code=500)

        # 세션 초기화
        new_session_key = str(uuid.uuid4())
        game_state_instance = GameState()
        game_state_instance.config['title'] = scenario_json.get('title')

        scenario_id = scenario_json.get('id', 0)
        start_scene_id = pick_start_scene_id(scenario_json)

        world_state_instance = WorldState()
        world_state_instance.reset()
        world_state_instance.initialize_from_scenario(scenario_json)

        player_state = {
            "scenario_id": scenario_id,
            "current_scene_id": start_scene_id,
            "start_scene_id": start_scene_id,
            "player_vars": {},
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
            "_internal_flags": {},
            "stuck_count": 0
        }

        game_state_data = {
            "config": game_state_instance.config,
            "state": player_state
        }

        update_build_progress(status="completed", step="완료", detail="생성 완료!", progress=100)
        return {
            "status": "success",
            "filename": fid,
            "session_key": new_session_key,
            "game_state": game_state_data,
            **scenario_json
        }

    except Exception as e:
        logger.error(f"Init Error: {e}")
        update_build_progress(status="error", detail=str(e))
        return JSONResponse({"error": str(e)}, status_code=500)


# ==========================================
# [API 라우트] NPC 관리
# ==========================================
@api_router.post('/npc/generate')
async def generate_npc_api(data: NPCGenerateRequest, user: CurrentUser = Depends(get_current_user)):
    """
    [수정] NPC 생성 API - 토큰 과금 적용 및 Auth 요구
    """
    if not user.is_authenticated:
        return JSONResponse({"success": False, "error": "Login required"}, status_code=401)

    try:
        # [수정] user.id 전달
        npc_data = await run_in_threadpool(
            generate_single_npc,
            scenario_title=data.scenario_title,
            scenario_summary=data.scenario_summary,
            user_request=data.request,
            model_name=data.model,
            user_id=user.id
        )
        return {"success": True, "data": npc_data}
    except Exception as e:
        logger.error(f"Scene Generation Error: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@api_router.post('/scene/generate')
async def generate_scene_content_api(data: NPCGenerateRequest, user: CurrentUser = Depends(get_current_user)):
    """
    씬 내용 생성 API (builder_agent.py 사용)
    """
    if not user.is_authenticated:
        return JSONResponse({"success": False, "error": "Login required"}, status_code=401)

    try:
        # builder_agent.py의 씬 생성 함수 호출
        scene_data = await run_in_threadpool(
            generate_scene_content,
            scenario_title=data.scenario_title,
            scenario_summary=data.scenario_summary,
            user_request=data.request,
            model_name=data.model,
            user_id=user.id
        )
        return {"success": True, "data": scene_data}
    except Exception as e:
        logger.error(f"Scene Generation Error: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@api_router.post('/image/generate')
async def generate_image_api(data: ImageGenerateRequest, user: CurrentUser = Depends(get_current_user)):
    # [추가] 로그인 체크
    if not user.is_authenticated:
        return JSONResponse({"success": False, "error": "Login required"}, status_code=401)

    try:
        image_service = get_image_service()

        if not image_service.is_available:
            return JSONResponse({
                "success": False,
                "error": "이미지 생성 서비스를 사용할 수 없습니다. 관리자에게 문의하세요."
            }, status_code=503)

        # [수정] user.id 전달하여 토큰 과금
        result = await image_service.generate_image(
            user_id=user.id,  # Added
            image_type=data.image_type,
            description=data.description,
            scenario_id=data.scenario_id,
            target_id=data.target_id
        )

        if result:
            return {"success": True, "data": result}
        else:
            return JSONResponse({
                "success": False,
                "error": "이미지 생성에 실패했습니다. (잔액 부족 또는 오류)"
            }, status_code=500)

    except Exception as e:
        logger.error(f"Image Generation Error: {e}")
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
    """레거시 프리셋 로드 API (사용 빈도 낮음 - 단순 메시지 반환)"""
    try:
        preset = db.query(Preset).filter(Preset.filename == filename).first()
        if not preset: return HTMLResponse('<div class="error">로드 실패</div>')
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

    # 자동 히스토리 추가 (성공 시에만)
    history_success, history_error = HistoryService.add_history(scenario_id, user.id, "draft_save", "Draft 저장", data)
    if not history_success:
        logger.warning(f"History save failed: {history_error}")
    
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

    try:
        cost = TokenConfig.COST_AI_AUDIT
        UserService.deduct_tokens(
            user_id=user.id,
            cost=cost,
            action_type="ai_audit",
            model_name=data.model
        )
        logger.info(f"💰 Audit token deducted for {user.id}: -{cost}")
    except ValueError as e:
        logger.warning(f"🚫 Audit 거부 (잔액 부족): {user.id} - {e}")
        return JSONResponse({"success": False, "error": "Insufficient tokens"}, status_code=402)
    except Exception as e:
        logger.error(f"❌ Audit 토큰 처리 중 오류: {e}")
        return JSONResponse({"success": False, "error": "Token processing failed"}, status_code=500)

    audit_result = await run_in_threadpool(method, result['scenario'], data.scene_id, data.model)

    return {"success": True, "audit_type": data.audit_type, "result": audit_result}


@api_router.post('/audit/scene')
async def audit_builder_scene(data: BuilderAuditRequest, user: CurrentUser = Depends(get_current_user)):
    """
    AI 검수 API (토큰 과금 포함)
    """
    if not user.is_authenticated:
        return JSONResponse({"success": False, "error": "Login required"}, status_code=401)

    try:
        nodes = data.scenario.get('nodes', [])
        edges = data.scenario.get('edges', [])

        # 1. 그래프 데이터를 시나리오 구조로 변환
        scenes, endings = MermaidService.convert_nodes_to_scenes(nodes, edges)

        temp_scenario = {
            "title": "Draft Audit",
            "scenes": scenes,
            "endings": endings
        }

        results = []

        # 2-A. 단일 씬 검수
        if data.scene_id:
            audit_res = await run_in_threadpool(
                AIAuditService.full_audit,
                temp_scenario,
                data.scene_id,
                data.model
            )
            # 프론트엔드 통일성을 위해 리스트 형태 또는 단일 객체로 반환 (여기선 단일 객체 구조 유지하되 issue 취합)
            return {"success": True, "result": audit_res, "mode": "single"}

        # 2-B. 전체 시나리오 검수
        else:
            # 모든 씬에 대해 반복 수행
            # (실제 서비스에서는 비동기 병렬 처리가 좋으나, 여기선 순차 처리로 안전하게 구현)
            combined_issues = {"coherence": {"issues": []}, "trigger": {"issues": []}}

            for scene in scenes:
                res = await run_in_threadpool(
                    AIAuditService.full_audit,
                    temp_scenario,
                    scene['scene_id'],
                    data.model
                )
                if res.get('coherence', {}).get('issues'):
                    combined_issues['coherence']['issues'].extend(res['coherence']['issues'])
                if res.get('trigger', {}).get('issues'):
                    combined_issues['trigger']['issues'].extend(res['trigger']['issues'])

            return {
                "success": True,
                "result": combined_issues,
                "mode": "full",
                "summary": f"전체 {len(scenes)}개 씬 검수 완료"
            }

    except Exception as e:
        logger.error(f"Builder Audit Error: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


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


@api_router.get('/item/list')
async def get_item_list(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """사용자가 생성한 아이템 목록 조회"""
    if not user.is_authenticated:
        return JSONResponse({"success": False, "error": "로그인이 필요합니다."}, status_code=401)
    try:
        # type이 'item'인 것만 조회
        items = db.query(CustomNPC).filter(
            CustomNPC.author_id == user.id,
            CustomNPC.type == 'item'
        ).order_by(CustomNPC.created_at.desc()).all()

        results = []
        for item in items:
            data = item.data if item.data else {}
            results.append({
                "id": item.id,
                "name": item.name,
                "type": data.get('type', 'ITEM'),
                "description": data.get('description', ''),
                "data": data
            })
        return results
    except Exception as e:
        logger.error(f"Item List Error: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
# ==========================================
# [API ?�우?? 관리자 기능 (?�나리오 ?�택�??�양)
# ==========================================

import json

RIGHTS_FILE = "scenario_rights.json"

def get_rights_holder():
    if os.path.exists(RIGHTS_FILE):
        try:
            with open(RIGHTS_FILE, 'r') as f:
                data = json.load(f)
                return data.get('holder', '11')
        except:
            return '11'
    return '11'

def set_rights_holder(user_id):
    with open(RIGHTS_FILE, 'w') as f:
        json.dump({'holder': user_id}, f)


@api_router.get('/admin/transfer_view', response_class=HTMLResponse)
async def admin_transfer_view(request: Request, user: CurrentUser = Depends(get_current_user)):
    # 11�??��? ?�인
    if user.id != '11':
         return HTMLResponse("<div class='p-4 text-red-500'>?�근 권한???�습?�다. (Only for 11)</div>")
    
    current_holder = get_rights_holder()
    
    html = f"""
    <div class="fade-in">
        <h2 class="text-xl font-bold text-yellow-400 mb-6 flex items-center gap-2">
            <i data-lucide="crown" class="w-6 h-6"></i> Scenario Selection Rights
        </h2>
        
        <div class="bg-rpg-800/80 p-6 rounded-2xl border border-yellow-500/30 mb-8">
            <div class="text-gray-400 text-sm mb-2">?�재 권한 보유??/div>
            <div class="text-2xl font-bold text-white flex items-center gap-3">
                <div class="w-10 h-10 rounded-full bg-yellow-500/20 flex items-center justify-center text-yellow-500">
                    <i data-lucide="user" class="w-6 h-6"></i>
                </div>
                {current_holder}
            </div>
            <p class="mt-4 text-sm text-gray-500">
                ?�나리오 ?�택권을 가�??��???메인 ?�면??추천 ?�나리오�??�정?????�습?�다. (?�정)
            </p>
        </div>

        <div class="space-y-4">
            <h3 class="text-lg font-bold text-white">권한 ?�양</h3>
            <p class="text-xs text-gray-400">?�이?��? 검?�하??권한???�길 ?��?�??�택?�세??</p>
            <div class="flex gap-2">
                <input type="text" name="search" id="user-search" 
                       placeholder="?��? ID 검??.." 
                       class="flex-1 bg-rpg-900 border border-rpg-700 rounded-lg px-4 py-3 text-white focus:border-yellow-500 outline-none font-sans"
                       hx-post="/api/admin/search_users" 
                       hx-trigger="keyup changed delay:500ms" 
                       hx-target="#user-search-results">
            </div>
            
            <div id="user-search-results" class="space-y-2 mt-4 max-h-60 overflow-y-auto custom-scrollbar">
                <!-- 검??결과 ?�시 ?�역 -->
            </div>
        </div>
    </div>
    <script>lucide.createIcons();</script>
    """
    return HTMLResponse(html)


@api_router.post("/admin/search_users", response_class=HTMLResponse)
async def search_users(request: Request, search: str = Form(None), db: Session = Depends(get_db)):
    if not search:
        return HTMLResponse('')
    
    # 본인 ?�외, 관리자(11) ?�외 검??
    users = db.query(User).filter(
        User.id.ilike(f"%{search}%"),
        User.id != '11'
    ).limit(5).all()
    
    if not users:
        return HTMLResponse('<div class="text-gray-500 text-sm p-4 text-center">검??결과가 ?�습?�다.</div>')
        
    html = ""
    for u in users:
        html += f"""
        <div class="flex items-center justify-between p-3 bg-rpg-900 rounded-lg border border-rpg-700 animate-fade-in">
            <div class="flex items-center gap-3">
                <div class="w-8 h-8 rounded-full bg-gray-700 flex items-center justify-center text-xs overflow-hidden">
                    {f'<img src="{u.avatar_url}" class="w-full h-full object-cover">' if u.avatar_url else u.id[:2]}
                </div>
                <span class="font-bold">{u.id}</span>
            </div>
            <button class="px-3 py-1 bg-yellow-600 hover:bg-yellow-500 text-white text-xs font-bold rounded transition-colors"
                    hx-post="/api/admin/transfer_rights"
                    hx-vals='{{"target_user_id": "{u.id}"}}'
                    hx-confirm="{u.id} ?�에�??�나리오 ?�택권을 ?�양?�시겠습?�까?">
                ?�양?�기
            </button>
        </div>
        """
    return HTMLResponse(html)


@api_router.post("/admin/transfer_rights")
async def transfer_rights(target_user_id: str = Form(...), user: CurrentUser = Depends(get_current_user)):
    # 11�??�용?�만 ?�행 가??
    if user.id != '11':
        return JSONResponse({"success": False, "error": "권한???�습?�다."}, status_code=403)
        
    set_rights_holder(target_user_id)
    
    return HTMLResponse(f"""
        <script>
            alert('{target_user_id} ?�에�?권한???�공?�으�??�양?�었?�니??');
            htmx.ajax('GET', '/api/admin/transfer_view', '#main-content-area');
        </script>
    """)
