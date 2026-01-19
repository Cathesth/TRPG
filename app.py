import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Depends, APIRouter
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from dotenv import load_dotenv

from config import LOG_FORMAT, LOG_DATE_FORMAT, get_full_version
from models import create_tables

# [중요] 작성하신 api.py를 가져오기 위한 임포트 (이게 없어서 빨간줄 발생)
from routes import api
from models import Base, engine # DB 모델 초기화용

# [추가] 뷰 로직 처리를 위한 서비스 Import
from services.mermaid_service import MermaidService
from core.state import GameState
from routes.auth import get_current_user_optional, CurrentUser

# 환경 변수 로드
load_dotenv()

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=LOG_DATE_FORMAT
)
logger = logging.getLogger(__name__)


# Lifespan 컨텍스트 (앱 시작/종료 시 실행)
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 앱 시작 시 DB 테이블 생성
    try:
        create_tables()
        logger.info("DB Tables created successfully.")
    except Exception as e:
        logger.error(f"DB Creation Failed: {e}")

    # S3 클라이언트 초기화
    try:
        from core.s3_client import get_s3_client
        s3_client = get_s3_client()
        await s3_client.initialize()
        logger.info("✅ S3 Client initialized.")
    except Exception as e:
        logger.error(f"❌ S3 Initialization Failed: {e}")

    # Vector DB 클라이언트 초기화
    try:
        from core.vector_db import get_vector_db_client
        vector_db = get_vector_db_client()
        await vector_db.initialize()
        logger.info("✅ Vector DB Client initialized.")
    except Exception as e:
        logger.error(f"❌ Vector DB Initialization Failed: {e}")

    yield

    # 앱 종료 시 Vector DB 연결 종료
    try:
        from core.vector_db import get_vector_db_client
        vector_db = get_vector_db_client()
        await vector_db.close()
        logger.info("👋 Vector DB connection closed.")
    except Exception as e:
        logger.error(f"❌ Vector DB Close Failed: {e}")


# FastAPI 앱 초기화
app = FastAPI(
    title="TRPG Studio",
    description="TRPG 시나리오 빌더 및 플레이어",
    version="1.0.0",
    lifespan=lifespan
)


# static/avatars 폴더가 없으면 생성하고, /static 경로로 접근 가능하게 설정
os.makedirs("static/avatars", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# 3. DB 테이블 생성 (앱 시작 시 자동 생성)
Base.metadata.create_all(bind=engine)

# HTTPS 프록시 미들웨어 (Railway 등 프록시 환경 대응)
class HTTPSMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # 프록시 헤더 확인 후 스키마 강제 고정
        if request.headers.get("x-forwarded-proto") == "https":
            request.scope["scheme"] = "https"
        return await call_next(request)

app.add_middleware(HTTPSMiddleware)

# [수정 1] 세션 미들웨어 (CORSMiddleware와 섞여있던 부분 정리)
# secret_key 변수를 여기서 정의해서 사용하거나 os.getenv를 직접 사용
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-me")

# 세션 미들웨어 (쿠키 기반 세션)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", "dev-secret-key-change-me"),
    max_age=86400 * 7,  # 7일
    same_site="lax",
    https_only=os.getenv("RAILWAY_ENVIRONMENT") is not None  # Railway에서는 HTTPS 강제
)

# CORS 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 캐시 방지 미들웨어
@app.middleware("http")
async def add_no_cache_header(request: Request, call_next):
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, post-check=0, pre-check=0, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '-1'
    return response


# 템플릿 설정
templates = Jinja2Templates(directory="templates")

# =================================================================
# [수정 시작] 라우터 등록 (Import 방식 변경)
# routes/__init__.py를 거치지 않고, 각 파일에서 직접 가져와 에러를 방지합니다.
# =================================================================

# 라우터 등록
from routes import api_router, game_router, views_router
# [추가] api.py에 정의한 mypage_router를 직접 가져옵니다.
#from routes.api import mypage_router

# [새 코드] 각 파일에서 직접 Import
#from routes.views import views_router
from routes.game import game_router
from routes.api import api_router, mypage_router


# [추가] Vector DB 라우터 등록
#from routes.vector_api import router as vector_router (아래 try-except에서 처리함)

app.include_router(views_router)
app.include_router(api_router)
app.include_router(game_router)

# [중요] 마이페이지 라우터를 명시적으로 등록하여 404 에러 해결
app.include_router(mypage_router)



# [S3] Assets 라우터 등록
#app.include_router(assets_router) # <----- 삭제필요 (변수 정의 안됨, 아래쪽 try-except에서 안전하게 등록함)

# [Vector DB] Vector DB 라우터 등록
#app.include_router(vector_router) # <----- 삭제필요 (변수 정의 안됨 혹은 중복 등록)

# [추가] 4. 라우터 등록 (api.py 연결)
# 여기서 api.api_router를 연결합니다.
#app.include_router(api.api_router) <----- 삭제필요 (위에서 app.include_router(api_router)로 이미 등록됨)
#app.include_router(api.mypage_router) # 마이페이지 라우터도 등록 <----- 삭제필요 (위에서 app.include_router(mypage_router)로 이미 등록됨)

# 3. [선택] Assets 라우터 (파일이 없어도 에러 안 나게 처리)
try:
    from routes.assets import router as assets_router
    app.include_router(assets_router)
    logger.info("✅ Assets router loaded.")
except ImportError:
    logger.warning("⚠️ routes.assets module not found. Assets router skipped.")

# 4. [Vector DB] 라우터 (파일이 없을 경우 대비하여 try-except 처리 권장)
try:
    from routes.vector_api import router as vector_router
    app.include_router(vector_router)
    logger.info("✅ Vector DB router loaded.")
except ImportError:
    logger.warning("routes.vector_api module not found. Vector DB router skipped.")


# Health check 엔드포인트 (Railway 모니터링용)
@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "TRPG Studio"}

@app.get("/")
async def root():
    return RedirectResponse(url="/views/main") # 또는 index.html 경로


if __name__ == '__main__':
    import uvicorn
    port = int(os.getenv("PORT", 5001))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)


