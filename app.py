import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from dotenv import load_dotenv

from config import LOG_FORMAT, LOG_DATE_FORMAT
from models import create_tables

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
    yield
    # 앱 종료 시 처리 (필요 시)


# FastAPI 앱 초기화
app = FastAPI(
    title="TRPG Studio",
    description="TRPG 시나리오 빌더 및 플레이어",
    version="1.0.0",
    lifespan=lifespan
)

# HTTPS 프록시 미들웨어 (Railway 등 프록시 환경 대응)
class HTTPSMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # 프록시 헤더 확인 후 스키마 강제 고정
        if request.headers.get("x-forwarded-proto") == "https":
            request.scope["scheme"] = "https"
        return await call_next(request)

app.add_middleware(HTTPSMiddleware)

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


# 정적 파일 서빙 (static 폴더가 있는 경우)
if os.path.exists(os.path.join(os.path.dirname(__file__), 'static')):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# 템플릿 설정
templates = Jinja2Templates(directory="templates")

# 라우터 등록
from routes import api_router, game_router, views_router


# [추가] api.py에 정의한 mypage_router를 직접 가져옵니다.
from routes.api import mypage_router

app.include_router(views_router)
app.include_router(api_router)
app.include_router(game_router)


# [중요] 마이페이지 라우터를 명시적으로 등록하여 404 에러 해결
app.include_router(mypage_router)


# Health check 엔드포인트 (Railway 모니터링용)
@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "TRPG Studio"}


if __name__ == '__main__':
    import uvicorn
    port = int(os.getenv("PORT", 5001))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)
