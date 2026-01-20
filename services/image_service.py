"""
AI 이미지 생성 서비스 (Together AI - Flux.1-schnell)
IP 차단 없음, 정식 API Key 사용, 고퀄리티 Flux 모델 지원
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
        # [필수] Railway에 TOGETHER_API_KEY 설정 필요
        self.api_key = os.getenv("TOGETHER_API_KEY")

        # Together AI 엔드포인트
        self.api_url = "https://api.together.xyz/v1/images/generations"

        # [모델] Flux.1-schnell (빠르고 퀄리티 최상)
        self.model = "black-forest-labs/FLUX.1-schnell"

        # 프롬프트 템플릿
        self.prompts = {
            "npc": "pixel art portrait of {description}, 8-bit, retro rpg style, white background, centered, clean lines, high quality, sharp focus",
            "enemy": "pixel art monster of {description}, 8-bit, retro rpg style, white background, intimidating, clean lines, high quality",
            "background": "pixel art landscape of {description}, 8-bit, retro rpg style, detailed environment, atmospheric, 16:9 aspect ratio"
        }

        if not self.api_key:
            logger.error("❌ [Image] TOGETHER_API_KEY가 없습니다. Together AI에서 발급받으세요.")
            self._is_available = False
        else:
            self._is_available = True
            logger.info(f"✅ [Image] 서비스 초기화 (Provider: Together AI, Model: Flux.1)")

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
            image_data = await self._call_together_api(prompt)

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
            logger.error(f"❌ [Image] 생성 오류: {e}")
            return None

    async def _call_together_api(self, prompt: str) -> Optional[bytes]:
        """Together AI API 호출"""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": self.model,
            "prompt": prompt,
            "width": 1024,
            "height": 1024,
            "steps": 4, # Flux Schnell은 4스텝이면 충분
            "n": 1,
            "response_format": "base64" # Base64로 받아서 바이너리로 변환
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url, headers=headers, json=payload, timeout=30.0) as response:
                    if response.status != 200:
                        err = await response.text()
                        logger.error(f"❌ [Image] Together API 오류 ({response.status}): {err}")
                        return None

                    result = await response.json()

                    # Base64 디코딩
                    import base64
                    b64_data = result['data'][0]['b64_json']
                    return base64.b64decode(b64_data)

        except Exception as e:
            logger.error(f"❌ [Image] 연결 실패: {e}")
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