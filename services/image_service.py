"""
AI 이미지 생성 서비스 (Hugging Face SDXL 1.0 - 가장 안정적)
Railway 환경에서 MiniO에 이미지 저장/로드 지원
"""
import os
import logging
import asyncio
import aiohttp
import uuid
from datetime import datetime
from typing import Optional, Dict, Any

from core.s3_client import get_s3_client

logger = logging.getLogger(__name__)

class ImageService:
    """AI 이미지 생성 및 관리 서비스"""

    def __init__(self):
        self.s3_client = get_s3_client()
        self.hf_token = os.getenv("HF_TOKEN")

        # [모델] Stability AI의 SDXL 1.0 (Flux보다 훨씬 안정적임)
        # 410 오류 방지를 위해 router 주소 사용
        self.api_url = "https://router.huggingface.co/models/stabilityai/stable-diffusion-xl-base-1.0"

        # 프롬프트 템플릿
        self.prompts = {
            "npc": "pixel art portrait of {description}, 8-bit, retro rpg style, white background, centered, clean lines, high quality",
            "enemy": "pixel art monster of {description}, 8-bit, retro rpg style, white background, intimidating, clean lines, high quality",
            "background": "pixel art landscape of {description}, 8-bit, retro rpg style, detailed environment, atmospheric, 16:9 aspect ratio"
        }

        if not self.hf_token:
            logger.warning("⚠️ [Image] HF_TOKEN이 없습니다. 이미지 생성이 불가능합니다.")
            self._is_available = False
        else:
            self._is_available = True
            logger.info(f"✅ [Image] 서비스 초기화 (Model: SDXL 1.0)")

    @property
    def is_available(self) -> bool:
        return self._is_available and self.s3_client.is_available

    async def generate_image(self, image_type: str, description: str, scenario_id: Optional[int] = None, target_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if not self.is_available or image_type not in self.prompts:
            return None

        try:
            prompt = self.prompts[image_type].format(description=description)
            logger.info(f"🎨 [Image] 생성 요청: {prompt[:50]}...")

            # API 호출
            image_data = await self._call_huggingface_api(prompt)

            if not image_data:
                return None

            # S3 업로드
            image_url = await self._upload_to_s3(image_data, image_type, scenario_id, target_id)

            if not image_url:
                return None

            return {
                "success": True,
                "image_url": image_url,
                "image_type": image_type,
                "description": description,
                "generated_at": datetime.now().isoformat()
            }
        except Exception as e:
            logger.error(f"❌ [Image] 생성 프로세스 오류: {e}")
            return None

    async def _call_huggingface_api(self, prompt: str) -> Optional[bytes]:
        """Hugging Face API 호출 (Retry 로직 포함)"""
        headers = {"Authorization": f"Bearer {self.hf_token}"}
        payload = {"inputs": prompt}

        # 모델이 'Cold Boot' 상태일 때 503 에러가 날 수 있음 -> 최대 3번 재시도
        for attempt in range(3):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(self.api_url, headers=headers, json=payload, timeout=60.0) as response:

                        if response.status == 200:
                            logger.info("✅ [Image] 생성 성공")
                            return await response.read()

                        error_msg = await response.text()

                        # 503: 모델 로딩 중 (흔한 경우)
                        if response.status == 503:
                            wait_time = 10
                            logger.info(f"⏳ [Image] 모델 로딩 중... {wait_time}초 대기 후 재시도 ({attempt+1}/3)")
                            await asyncio.sleep(wait_time)
                            continue

                        logger.error(f"❌ [Image] API 오류 ({response.status}): {error_msg}")
                        return None

            except Exception as e:
                logger.error(f"❌ [Image] 연결 실패: {e}")
                return None

        return None

    async def _upload_to_s3(self, image_data: bytes, image_type: str, scenario_id: Optional[int] = None, target_id: Optional[str] = None) -> Optional[str]:
        try:
            folder = f"ai-images/{scenario_id}/{image_type}" if scenario_id else f"ai-images/{image_type}"
            filename = f"{target_id or 'generated'}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{str(uuid.uuid4())[:8]}.png"
            return await self.s3_client.upload_file(image_data, filename, "image/png", folder)
        except Exception as e:
            logger.error(f"❌ [Image] S3 업로드 실패: {e}")
            return None

    async def delete_image(self, image_url: str) -> bool:
        if not self.s3_client.is_available or "/" not in image_url: return False
        try:
            s3_key = image_url.split("/", 3)[-1]
            return await self.s3_client.delete_file(s3_key)
        except: return False

_image_service: Optional[ImageService] = None
def get_image_service() -> ImageService:
    global _image_service
    if _image_service is None: _image_service = ImageService()
    return _image_service