"""
에셋 관리 API 엔드포인트 (이미지 업로드 등)
FastAPI 비동기 스트리밍 방식 구현
"""
from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
from typing import Optional
import logging

from core.s3_client import get_s3_client

router = APIRouter(prefix="/api/assets", tags=["Assets"])
logger = logging.getLogger(__name__)


@router.post("/upload-image")
async def upload_image(
    file: UploadFile = File(..., description="업로드할 이미지 파일")
):
    """
    이미지를 S3에 업로드하고 접근 URL 반환

    **사용 예시:**
    ```bash
    curl -X POST "http://localhost:8000/api/assets/upload-image" \
         -F "file=@/path/to/image.png"
    ```

    **응답 예시:**
    ```json
    {
        "success": true,
        "url": "https://s3.example.com/trpg-assets/uploads/20260115_abc123.png",
        "filename": "image.png",
        "size": 102400
    }
    ```
    """
    s3_client = get_s3_client()

    # S3가 구성되지 않은 경우
    if not s3_client.is_available:
        raise HTTPException(
            status_code=503,
            detail="S3 스토리지가 구성되지 않았습니다. 관리자에게 문의하세요."
        )

    # Content-Type 검증 (이미지 파일만 허용)
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(
            status_code=400,
            detail=f"이미지 파일만 업로드 가능합니다. (현재: {file.content_type})"
        )

    try:
        # 스트리밍 방식으로 파일 데이터 읽기
        file_data = await file.read()
        file_size = len(file_data)

        # 파일 크기 제한 (10MB)
        max_size = 10 * 1024 * 1024  # 10MB
        if file_size > max_size:
            raise HTTPException(
                status_code=400,
                detail=f"파일 크기가 너무 큽니다. (최대: 10MB, 현재: {file_size / 1024 / 1024:.2f}MB)"
            )

        logger.info(f"📤 [UPLOAD] 파일 업로드 시작: {file.filename} ({file_size} bytes)")

        # S3에 업로드
        file_url = await s3_client.upload_file(
            file_data=file_data,
            filename=file.filename,
            content_type=file.content_type,
            folder="uploads"
        )

        if not file_url:
            raise HTTPException(
                status_code=500,
                detail="파일 업로드에 실패했습니다."
            )

        logger.info(f"✅ [UPLOAD] 업로드 성공: {file_url}")

        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "url": file_url,
                "filename": file.filename,
                "size": file_size,
                "content_type": file.content_type
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ [UPLOAD] 업로드 중 오류 발생: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"업로드 중 오류가 발생했습니다: {str(e)}"
        )


@router.post("/upload-scenario-image")
async def upload_scenario_image(
    file: UploadFile = File(..., description="시나리오 관련 이미지"),
    scenario_id: Optional[int] = None
):
    """
    시나리오 관련 이미지 업로드 (장면 이미지, NPC 초상화 등)

    시나리오 ID가 제공되면 'scenario_{id}/' 폴더에 저장
    """
    s3_client = get_s3_client()

    if not s3_client.is_available:
        raise HTTPException(
            status_code=503,
            detail="S3 스토리지가 구성되지 않았습니다."
        )

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(
            status_code=400,
            detail=f"이미지 파일만 업로드 가능합니다."
        )

    try:
        file_data = await file.read()
        file_size = len(file_data)

        # 20MB 제한 (고화질 시나리오 이미지)
        max_size = 20 * 1024 * 1024
        if file_size > max_size:
            raise HTTPException(
                status_code=400,
                detail=f"파일 크기가 너무 큽니다. (최대: 20MB)"
            )

        # 폴더 경로 설정
        folder = f"scenarios/scenario_{scenario_id}" if scenario_id else "scenarios/general"

        logger.info(f"📤 [SCENARIO UPLOAD] {file.filename} -> {folder}")

        file_url = await s3_client.upload_file(
            file_data=file_data,
            filename=file.filename,
            content_type=file.content_type,
            folder=folder
        )

        if not file_url:
            raise HTTPException(
                status_code=500,
                detail="파일 업로드에 실패했습니다."
            )

        logger.info(f"✅ [SCENARIO UPLOAD] 성공: {file_url}")

        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "url": file_url,
                "filename": file.filename,
                "size": file_size,
                "scenario_id": scenario_id,
                "folder": folder
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ [SCENARIO UPLOAD] 오류: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"업로드 중 오류가 발생했습니다: {str(e)}"
        )


@router.get("/health")
async def check_s3_health():
    """S3 스토리지 상태 확인"""
    s3_client = get_s3_client()

    return {
        "s3_available": s3_client.is_available,
        "s3_initialized": s3_client._initialized,
        "bucket": s3_client.bucket if s3_client.is_available else None,
        "endpoint": s3_client.endpoint if s3_client.is_available else None
    }

