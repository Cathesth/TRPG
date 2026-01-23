import os
import logging
from dotenv import load_dotenv

# [핵심 수정] 환경 변수를 가장 먼저 로드해야 다른 파일들이 이 변수를 쓸 수 있습니다.
load_dotenv()

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Depends, APIRouter
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, RedirectResponse, StreamingResponse, HTMLResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware


from config import LOG_FORMAT, LOG_DATE_FORMAT, get_full_version

# 점검 페이지 HTML (위트 있는 TRPG 컨셉)
MAINTENANCE_HTML = """
<html>
    <head><title>점검 중</title></head>
    <body style="text-align:center; padding-top:100px; font-family: sans-serif;">
        <h1>🎲 다이스 갓이 주사위를 다시 굴리고 있습니다...</h1>
        <p>현재 서버 점검 중입니다. 잠시 후 다시 모험을 시작해주세요.</p>
        <p style="color: gray;">(GM이 시나리오 노트를 쏟았다는 소문이 있습니다.)</p>
    </body>
</html>
"""
from models import create_tables

# [중요] 작성하신 api.py를 가져오기 위한 임포트 (이게 없어서 빨간줄 발생)
from routes import api
from models import Base, engine # DB 모델 초기화용

# [추가] 뷰 로직 처리를 위한 서비스 Import
from services.mermaid_service import MermaidService
from core.state import GameState
from routes.auth import get_current_user_optional, CurrentUser

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
# FastAPI 앱 초기화
app = FastAPI(
    title="TRPG Studio",
    description="TRPG 시나리오 빌더 및 플레이어",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/my-secret-testing-docs", # [Security] API 문서 경로 변경
    redoc_url=None # [Security] Redoc 비활성화
)


# static/avatars 폴더가 없으면 생성하고, /static 경로로 접근 가능하게 설정
os.makedirs("static/avatars", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# 3. DB 테이블 생성 (앱 시작 시 자동 생성)
Base.metadata.create_all(bind=engine)

# 점검 모드 미들웨어
class MaintenanceMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # Railway 환경 변수 확인
        if os.environ.get("MAINTENANCE_MODE") == "true":
            # 점검 중일 때 503 Service Unavailable 반환
            return HTMLResponse(content=MAINTENANCE_HTML, status_code=503)
        
        response = await call_next(request)
        return response

# HTTPS 프록시 미들웨어 (Railway 등 프록시 환경 대응)
class HTTPSMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # 프록시 헤더 확인 후 스키마 강제 고정
        if request.headers.get("x-forwarded-proto") == "https":
            request.scope["scheme"] = "https"
        return await call_next(request)

app.add_middleware(MaintenanceMiddleware)
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
from routes import api_router, game_router, views_router, admin_router
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
app.include_router(admin_router) # [NEW] 관리자 라우터

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


@app.get("/image/serve/{file_path:path}")
async def serve_image(file_path: str):
    from core.s3_client import get_s3_client
    from fastapi.responses import Response, FileResponse
    import urllib.parse
    import botocore.exceptions

    # 0. 로컬 Static 파일인 경우 처리 (static/으로 시작하는 경우)
    if file_path.startswith("static/") or file_path.startswith("/static/"):
        local_path = file_path.lstrip("/")
        if os.path.exists(local_path):
            return FileResponse(local_path)

    s3 = get_s3_client()

    # S3 세션이 초기화되지 않았으면 초기화
    if not s3._session:
        await s3.initialize()

    # 환경변수 또는 S3 클라이언트 설정에서 버킷명 가져오기
    bucket_name = s3.bucket

    try:
        # 1. URL 디코딩 및 키 파싱
        decoded_path = urllib.parse.unquote(file_path)
        real_key = decoded_path

        # URL 형태인 경우 파싱 (http://... 또는 https://...)
        if "://" in decoded_path:
            parsed = urllib.parse.urlparse(decoded_path)
            # path 부분만 사용 (예: /bucket-name/path/to/image.png)
            full_path = parsed.path.lstrip('/')
            
            # 버킷명이 경로 앞에 포함되어 있다면 제거
            if full_path.startswith(f"{bucket_name}/"):
                real_key = full_path.replace(f"{bucket_name}/", "", 1)
            else:
                real_key = full_path
            
            # [FIX] URL 경로에 포함된 한글 등은 여전히 인코딩된 상태일 수 있으므로 한 번 더 디코딩
            # S3/MinIO 키는 보통 유니코드로 저장됨
            real_key = urllib.parse.unquote(real_key)
        
        # 디버그 로그
        # logger.info(f"🔍 [Image Serve] Request: {file_path} -> Decoded: {decoded_path} -> Key: {real_key}")

        # 2. S3 클라이언트 컨텍스트 생성 후 파일 읽기
        async with s3._session.client(
                's3',
                endpoint_url=s3.endpoint,
                region_name=s3.region,
                use_ssl=s3.use_ssl
        ) as client:
            try:
                response = await client.get_object(Bucket=bucket_name, Key=real_key)
                content = await response['Body'].read()
                return Response(content=content, media_type=response.get('ContentType', 'image/png'))
            except client.exceptions.NoSuchKey:
                # [FIX] 키 불일치 시 폴백 시도 (공백 <-> 언더바 치환)
                logger.warning(f"⚠️ [Image Serve] S3 Key Not Found: {real_key}. Retrying with variations...")
                
                # 변형 시도 리스트
                variations = []
                if '_' in real_key:
                    variations.append(real_key.replace('_', ' '))
                if ' ' in real_key:
                    variations.append(real_key.replace(' ', '_'))
                
                found_content = None
                found_type = None

                for var_key in variations:
                    try:
                        logger.info(f"🔄 [Image Serve] Retrying with key: {var_key}")
                        response = await client.get_object(Bucket=bucket_name, Key=var_key)
                        found_content = await response['Body'].read()
                        found_type = response.get('ContentType', 'image/png')
                        logger.info(f"✅ [Image Serve] Found with key: {var_key}")
                        break
                    except client.exceptions.NoSuchKey:
                        continue
                
                if found_content:
                    return Response(content=found_content, media_type=found_type)

                # 최종 실패
                logger.error(f"❌ [Image Serve] Final Failure. Key not found: {real_key}")
                return Response(status_code=404)
            except Exception as e:
                logger.error(f"❌ [Image Serve] S3 Error: {str(e)}")
                return Response(status_code=500)

    except Exception as e:
        logger.error(f"❌ [Image Serve] General Error: {str(e)} (Path: {file_path})")
        return Response(status_code=404)


@app.get("/trpg-assets/{file_path:path}")
async def proxy_trpg_assets(file_path: str):
    """
    MinIO 내부망 이미지를 외부에서 접근할 수 있도록 하는 중계(Proxy) 라우트
    URL: /trpg-assets/{file_path} -> MinIO: {bucket}/{file_path}
    """
    from core.s3_client import get_s3_client
    from fastapi.responses import Response

    s3 = get_s3_client()
    # 세션 초기화
    if not s3._session:
        await s3.initialize()

    # 버킷명 확인 (S3 클라이언트에 설정된 버킷 사용)
    bucket_name = s3.bucket 
    
    try:
        # [FIX] URL 디코딩
        import urllib.parse
        decoded_path = urllib.parse.unquote(file_path)
        
        async with s3._session.client(
                's3',
                endpoint_url=s3.endpoint,
                region_name=s3.region,
                use_ssl=s3.use_ssl
        ) as client:
            # 1. 원본 키로 시도
            try:
                response = await client.get_object(Bucket=bucket_name, Key=decoded_path)
                content = await response['Body'].read()
                return Response(content=content, media_type=response.get('ContentType', 'image/png'))
            except client.exceptions.NoSuchKey:
                # 2. 소문자로 변환하여 시도 (Linux FS 대응)
                try:
                    lower_key = decoded_path.lower()
                    if lower_key == decoded_path: raise Exception("Same key") # 이미 소문자면 패스
                    logger.info(f"⚠️ [Proxy] Retrying with lowercase key: {lower_key}")
                    response = await client.get_object(Bucket=bucket_name, Key=lower_key)
                    content = await response['Body'].read()
                    return Response(content=content, media_type=response.get('ContentType', 'image/png'))
                except:
                    # 3. 대문자로 시작하는 파일명 시도 (G-72.png) - 경로의 마지막 부분만 대문자화
                    try:
                        parts = decoded_path.split('/')
                        filename = parts[-1]
                        if filename:
                            # 첫 글자만 대문자로 (S3 업로드 시 자동 변경 가능성)
                            capitalized_filename = filename[0].upper() + filename[1:]
                            parts[-1] = capitalized_filename
                            cap_key = '/'.join(parts)
                            
                            if cap_key == decoded_path: raise Exception("Same key")
                            
                            logger.info(f"⚠️ [Proxy] Retrying with capitalized key: {cap_key}")
                            response = await client.get_object(Bucket=bucket_name, Key=cap_key)
                            content = await response['Body'].read()
                            return Response(content=content, media_type=response.get('ContentType', 'image/png'))
                    except:
                        pass
                        
                    logger.warning(f"⚠️ [Proxy] S3 Key Not Found (All attempts failed): {decoded_path}")
                    return Response(status_code=404)
            except Exception as e:
                logger.error(f"❌ [Proxy] S3 Error: {str(e)}")
                return Response(status_code=500)
    except Exception as e:
        logger.error(f"❌ [Proxy] General Error: {str(e)}")
        return Response(status_code=500)


if __name__ == '__main__':
    import uvicorn
    port = int(os.getenv("PORT", 5001))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)


