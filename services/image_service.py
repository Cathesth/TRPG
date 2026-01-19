"""
AI 이미지 생성 서비스 (Google Gemini 2.0 Flash 기반 - Free Tier 호환)
Railway 환경에서 MiniO에 이미지 저장/로드 지원
"""
import os
import logging
import asyncio
import uuid
import base64
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

        # [수정] AI Studio(무료)에서 이미지 생성이 가능한 최신 모델
        # "imagen-3.0-generate-002" 대신 Gemini 2.0 Flash를 사용합니다.
        self.model_name = "gemini-2.0-flash"

        self.prompts = {
            "npc": "Draw a high quality 8-bit pixel art portrait of {description}. Retro game character sprite style, white background, centered, clean lines, vibrant colors.",
            "enemy": "Draw a high quality 8-bit pixel art monster of {description}. Retro game enemy sprite style, intimidating, white background, clean lines.",
            "background": "Draw a high quality 8-bit pixel art landscape of {description}. Retro game background style, detailed environment, atmospheric."
        }

        if not self.api_key:
            logger.warning("⚠️ [Image] GOOGLE_API_KEY가 설정되지 않았습니다.")
            self._is_available = False
        else:
            try:
                self.client = genai.Client(api_key=self.api_key)
                self._is_available = True
                logger.info(f"✅ [Image] Google 서비스 초기화 완료 (Model: {self.model_name})")
            except Exception as e:
                logger.error(f"❌ [Image] 초기화 실패: {e}")
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

            # 동기 함수 실행
            image_bytes = await asyncio.to_thread(self._generate_with_gemini, prompt, image_type)

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

    def _generate_with_gemini(self, prompt: str, image_type: str) -> Optional[bytes]:
        """Gemini 2.0 Flash를 사용하여 이미지 생성"""
        try:
            # 1:1 비율 또는 16:9 비율 설정
            # Gemini 2.0 Flash는 '1:1', '3:4', '4:3', '9:16', '16:9' 지원
            aspect = "16:9" if image_type == "background" else "1:1"

            # [핵심] generate_content를 쓰되 response_modalities에 'IMAGE'를 포함
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                    # 일부 라이브러리 버전에 따라 image_aspect_ratio가 동작하지 않을 수 있으니
                    # 프롬프트에 비율을 명시하는 것이 더 안전할 수 있습니다.
                    # 여기서는 SDK 문법에 맞춰 시도합니다.
                )
            )

            # 응답에서 이미지 추출
            if response.candidates and response.candidates[0].content.parts:
                for part in response.candidates[0].content.parts:
                    # 인라인 데이터로 이미지가 들어오는 경우
                    if part.inline_data:
                        logger.info(f"✅ [Image] 이미지 생성 성공 ({len(part.inline_data.data)} bytes)")
                        return part.inline_data.data

                    # SDK 버전에 따라 executable_code 형태로 올 수도 있음 (드묾)

            logger.error("❌ [Image] 생성된 이미지 데이터가 없습니다.")
            return None

        except Exception as e:
            logger.error(f"❌ [Image] API 호출 실패: {e}")
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