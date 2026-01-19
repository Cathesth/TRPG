"""
AI 이미지 생성 서비스 (Google Imagen 3.0 API)
Railway 환경에서 MiniO에 이미지 저장/로드 지원
"""
import os
import logging
import asyncio
import uuid
from datetime import datetime
from typing import Optional, Dict, Any
from google import genai
from google.genai import types

from core.s3_client import get_s3_client

logger = logging.getLogger(__name__)

class ImageService:
    """AI 이미지 생성 및 관리 서비스"""

    def __init__(self):
        self.s3_client = get_s3_client()
        self.api_key = os.getenv("GOOGLE_API_KEY")

        # [모델] Google Imagen 3 (가장 최신/고품질)
        # 002가 안되면 001이 표준입니다.
        self.model_name = "imagen-3.0-generate-002"

        self.prompts = {
            "npc": "Draw a high quality 8-bit pixel art portrait of {description}. Retro game character sprite style, white background, centered, clean lines, vibrant colors.",
            "enemy": "Draw a high quality 8-bit pixel art monster of {description}. Retro game enemy sprite style, intimidating, white background, clean lines.",
            "background": "Draw a high quality 8-bit pixel art landscape of {description}. Retro game background style, detailed environment, atmospheric."
        }

        if not self.api_key:
            logger.warning("⚠️ [Image] GOOGLE_API_KEY가 없습니다.")
            self._is_available = False
        else:
            try:
                # 구글 클라이언트 초기화
                self.client = genai.Client(api_key=self.api_key)
                self._is_available = True
                logger.info(f"✅ [Image] Google Imagen 3 서비스 초기화 (Model: {self.model_name})")
            except Exception as e:
                logger.error(f"❌ [Image] 클라이언트 초기화 실패: {e}")
                self._is_available = False

    @property
    def is_available(self) -> bool:
        return self._is_available and self.s3_client.is_available

    async def generate_image(self, image_type: str, description: str, scenario_id: Optional[int] = None, target_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if not self.is_available or image_type not in self.prompts:
            return None

        try:
            prompt = self.prompts[image_type].format(description=description)
            logger.info(f"🎨 [Image] 생성 요청: {prompt[:50]}...")

            # API 호출 (동기 함수라 스레드풀 사용)
            image_bytes = await asyncio.to_thread(self._call_google_api, prompt, image_type)

            if not image_bytes:
                return None

            # S3 업로드
            image_url = await self._upload_to_s3(image_bytes, image_type, scenario_id, target_id)

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
            logger.error(f"❌ [Image] 프로세스 오류: {e}")
            return None

    def _call_google_api(self, prompt: str, image_type: str) -> Optional[bytes]:
        """Google Imagen API 호출"""
        try:
            aspect_ratio = "16:9" if image_type == "background" else "1:1"

            # [핵심] models.generate_images 메서드 사용
            response = self.client.models.generate_images(
                model=self.model_name,
                prompt=prompt,
                config=types.GenerateImagesConfig(
                    number_of_images=1,
                    aspect_ratio=aspect_ratio,
                    include_rai_reason=True,
                    output_mime_type="image/png"
                )
            )

            if response.generated_images:
                logger.info("✅ [Image] 생성 성공")
                return response.generated_images[0].image.image_bytes
            else:
                logger.error("❌ [Image] 생성된 이미지가 없습니다.")
                return None

        except Exception as e:
            # 오류 메시지 상세 로깅
            logger.error(f"❌ [Image] Google API 오류: {e}")
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