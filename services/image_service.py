"""
AI 이미지 생성 서비스 (Pollinations.ai 우회 설정 + HuggingFace 지원)
Railway 환경에서 MiniO에 이미지 저장/로드 지원
"""
import os
import logging
import asyncio
import aiohttp
import random
import uuid
from typing import Optional, Dict, Any
from datetime import datetime

from core.s3_client import get_s3_client

logger = logging.getLogger(__name__)

class ImageService:
    """AI 이미지 생성 및 관리 서비스"""

    def __init__(self):
        self.s3_client = get_s3_client()

        # 1. 기본: Pollinations.ai (무료, 키 불필요)
        self.provider = "pollinations"
        self.pollinations_url = "https://image.pollinations.ai/prompt"

        # 2. 예비: HuggingFace (무료 토큰 필요)
        # 만약 Pollinations가 Railway IP를 막으면 이걸 써야 함
        self.hf_token = os.getenv("HF_TOKEN") # .env에 HF_TOKEN=hf_... 추가 필요
        self.hf_api_url = "https://api-inference.huggingface.co/models/black-forest-labs/FLUX.1-schnell"

        # 이미지 생성 프롬프트 템플릿
        self.prompts = {
            "npc": "pixel art portrait of {description}, 8bit, retro game style, white background, centered, high quality, minimal details",
            "enemy": "pixel art monster of {description}, 8bit, enemy sprite, retro game style, white background, high quality",
            "background": "pixel art landscape of {description}, 8bit, game background, retro game style, detailed, 16:9 aspect ratio"
        }

        self._is_available = True
        logger.info(f"✅ [Image] 이미지 서비스 초기화 (기본: {self.provider})")

    @property
    def is_available(self) -> bool:
        return self._is_available and self.s3_client.is_available

    async def generate_image(self, image_type: str, description: str, scenario_id: Optional[int] = None, target_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if not self.is_available or image_type not in self.prompts:
            return None

        try:
            prompt = self.prompts[image_type].format(description=description)

            # 1차 시도: Pollinations
            image_data = await self._try_pollinations(prompt)

            # 2차 시도: HuggingFace (Pollinations 실패 시 백업)
            if not image_data and self.hf_token:
                logger.warning("⚠️ [Image] Pollinations 실패 -> HuggingFace로 재시도")
                image_data = await self._try_huggingface(prompt)

            if not image_data:
                logger.error("❌ [Image] 모든 이미지 생성 시도 실패")
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
            logger.error(f"❌ [Image] 생성 중 예외: {e}")
            return None

    async def _try_pollinations(self, prompt: str) -> Optional[bytes]:
        """Pollinations.ai 호출 (헤더 위장)"""
        try:
            seed = random.randint(0, 1000000)
            url = f"{self.pollinations_url}/{prompt}"

            params = {
                "width": 1024, "height": 1024,
                "seed": seed, "nologo": "true", "model": "flux"
            }

            # [중요] 브라우저인 척 위장하는 헤더
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://pollinations.ai/",
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, headers=headers, timeout=30.0) as response:
                    if response.status == 200:
                        content_type = response.headers.get("Content-Type", "")
                        # HTML이 오면 차단된 것임
                        if "text/html" in content_type:
                            logger.warning("⚠️ [Image] Pollinations가 Railway IP를 차단했습니다 (Cloudflare).")
                            return None
                        return await response.read()
                    else:
                        logger.warning(f"⚠️ [Image] Pollinations 오류: {response.status}")
                        return None
        except Exception as e:
            logger.error(f"⚠️ [Image] Pollinations 호출 중 오류: {e}")
            return None

    async def _try_huggingface(self, prompt: str) -> Optional[bytes]:
        """HuggingFace Inference API 호출 (백업용)"""
        try:
            headers = {"Authorization": f"Bearer {self.hf_token}"}
            payload = {"inputs": prompt}

            async with aiohttp.ClientSession() as session:
                async with session.post(self.hf_api_url, headers=headers, json=payload, timeout=30.0) as response:
                    if response.status == 200:
                        return await response.read()
                    else:
                        err = await response.text()
                        logger.error(f"⚠️ [Image] HuggingFace 오류: {response.status} - {err}")
                        return None
        except Exception as e:
            logger.error(f"⚠️ [Image] HuggingFace 호출 중 오류: {e}")
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