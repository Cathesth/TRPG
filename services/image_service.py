"""
AI 이미지 생성 서비스 (Lightweight SD 1.5 + External URL Fallback)
Railway 환경의 차단/타임아웃 문제를 회피하기 위한 하이브리드 방식
"""
import os
import logging
import asyncio
import aiohttp
import uuid
import random
import urllib.parse
from datetime import datetime
from typing import Optional, Dict, Any

from core.s3_client import get_s3_client

logger = logging.getLogger(__name__)

class ImageService:
    """AI 이미지 생성 및 관리 서비스"""

    def __init__(self):
        self.s3_client = get_s3_client()
        self.hf_token = os.getenv("HF_TOKEN")

        # [전략 1] Hugging Face SD 1.5 (가볍고 무료 서버에서 성공률 높음)
        self.hf_url = "https://router.huggingface.co/models/runwayml/stable-diffusion-v1-5"

        # [전략 2] Pollinations (백엔드 차단 시 URL만이라도 쓰기 위함)
        self.pollinations_base = "https://pollinations.ai/p"

        self.prompts = {
            "npc": "pixel art portrait of {description}, 8-bit, retro game, white background, centered, clean lines, high quality",
            "enemy": "pixel art monster of {description}, 8-bit, retro game, white background, intimidating, clean lines",
            "background": "pixel art landscape of {description}, 8-bit, retro game, detailed, atmospheric"
        }

        self._is_available = True
        logger.info(f"✅ [Image] 서비스 초기화 (Model: SD 1.5 + Fallback)")

    @property
    def is_available(self) -> bool:
        return self._is_available # S3가 죽어도 외부 URL로라도 보여주기 위해 True 유지

    async def generate_image(self, image_type: str, description: str, scenario_id: Optional[int] = None, target_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if image_type not in self.prompts:
            return None

        try:
            # 1. 프롬프트 준비
            prompt = self.prompts[image_type].format(description=description)
            logger.info(f"🎨 [Image] 생성 시도: {prompt[:30]}...")

            image_data = None
            final_image_url = None

            # 2. [1순위] Hugging Face (SD 1.5) 시도
            if self.hf_token:
                image_data = await self._try_huggingface(prompt)

            # 3. [2순위] Pollinations 직접 다운로드 시도
            if not image_data:
                logger.warning("⚠️ HF 실패 -> Pollinations 다운로드 시도")
                image_data = await self._try_pollinations_download(prompt)

            # 4. S3 업로드 시도 (데이터가 있을 경우)
            if image_data:
                if self.s3_client.is_available:
                    final_image_url = await self._upload_to_s3(image_data, image_type, scenario_id, target_id)

            # 5. [최후의 수단] 이미지 데이터 획득 실패했거나 S3 업로드 실패 시 -> 외부 URL 직접 반환
            # Railway가 차단당해도 사용자는 이미지를 볼 수 있음
            if not final_image_url:
                logger.warning("⚠️ 서버 저장 실패 -> 외부 URL(Pollinations) 직접 반환")
                seed = random.randint(0, 10000)
                encoded_prompt = urllib.parse.quote(prompt)
                final_image_url = f"{self.pollinations_base}/{encoded_prompt}?width=1024&height=1024&seed={seed}&nologo=true&model=flux"

            return {
                "success": True,
                "image_url": final_image_url,
                "image_type": image_type,
                "description": description,
                "generated_at": datetime.now().isoformat()
            }

        except Exception as e:
            logger.error(f"❌ [Image] 치명적 오류: {e}")
            # 에러가 나도 죽지 않고 외부 URL이라도 던져줌
            encoded_prompt = urllib.parse.quote(self.prompts[image_type].format(description=description))
            fallback_url = f"{self.pollinations_base}/{encoded_prompt}?nologo=true"
            return {
                "success": True,
                "image_url": fallback_url,
                "image_type": image_type,
                "description": description,
                "generated_at": datetime.now().isoformat()
            }

    async def _try_huggingface(self, prompt: str) -> Optional[bytes]:
        """SD 1.5 호출"""
        headers = {"Authorization": f"Bearer {self.hf_token}"}
        payload = {"inputs": prompt}

        for _ in range(3): # 3번 재시도
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(self.hf_url, headers=headers, json=payload, timeout=30.0) as response:
                        if response.status == 200:
                            return await response.read()
                        elif response.status == 503:
                            await asyncio.sleep(5)
                            continue
                        else:
                            break
            except:
                pass
        return None

    async def _try_pollinations_download(self, prompt: str) -> Optional[bytes]:
        """Pollinations 다운로드 시도 (User-Agent 위장)"""
        try:
            encoded_prompt = urllib.parse.quote(prompt)
            url = f"{self.pollinations_base}/{encoded_prompt}?width=1024&height=1024&nologo=true"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=30.0) as response:
                    if response.status == 200 and "image" in response.headers.get("Content-Type", ""):
                        return await response.read()
        except:
            pass
        return None

    async def _upload_to_s3(self, image_data: bytes, image_type: str, scenario_id: Optional[int] = None, target_id: Optional[str] = None) -> Optional[str]:
        try:
            folder = f"ai-images/{scenario_id}/{image_type}" if scenario_id else f"ai-images/{image_type}"
            filename = f"{target_id or 'generated'}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{str(uuid.uuid4())[:8]}.png"
            return await self.s3_client.upload_file(image_data, filename, "image/png", folder)
        except:
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