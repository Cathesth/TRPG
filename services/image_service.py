"""
AI 이미지 생성 서비스 (OpenRouter Chat API 호환)
Railway 환경에서 MiniO에 이미지 저장/로드 지원
"""
import os
import logging
import asyncio
import aiohttp
import re
from typing import Optional, Dict, Any
from datetime import datetime
import uuid
import base64

from core.s3_client import get_s3_client

logger = logging.getLogger(__name__)

class ImageService:
    """AI 이미지 생성 및 관리 서비스"""

    def __init__(self):
        self.s3_client = get_s3_client()
        # [중요] OpenRouter는 chat/completions를 통해 이미지 생성을 지원합니다.
        self.openrouter_api_url = "https://openrouter.ai/api/v1/chat/completions"
        self.openrouter_api_key = os.getenv("OPENROUTER_API_KEY")

        # [중요] OpenRouter에서 확실히 지원하는 FLUX 모델 사용 (가장 안정적)
        # 대안: google/gemini-2.0-flash-exp (이미지 생성 지원 시)
        self.image_model = os.getenv("OPENROUTER_IMAGE_MODEL", "black-forest-labs/flux-1-schnell")

        # 이미지 생성 프롬프트 템플릿
        self.prompts = {
            "npc": "Create an 8bit pixel art portrait of {description}, game character sprite, retro gaming style, white background, centered, high quality. Return ONLY the image URL.",
            "enemy": "Create an 8bit pixel art monster of {description}, enemy sprite, retro gaming style, intimidating, white background, high quality. Return ONLY the image URL.",
            "background": "Create an 8bit pixel art landscape of {description}, game background, retro gaming style, detailed environment, atmospheric, 16:9 aspect ratio. Return ONLY the image URL."
        }

        if not self.openrouter_api_key:
            logger.warning("⚠️ [Image] OPENROUTER_API_KEY가 설정되지 않았습니다.")
            self._is_available = False
        else:
            self._is_available = True
            logger.info(f"✅ [Image] OpenRouter 서비스 초기화 (Model: {self.image_model})")

    @property
    def is_available(self) -> bool:
        return self._is_available and self.s3_client.is_available

    async def generate_image(self, image_type: str, description: str, scenario_id: Optional[int] = None, target_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if not self.is_available or image_type not in self.prompts:
            return None

        try:
            prompt = self.prompts[image_type].format(description=description)
            image_data = await self._call_openrouter_api(prompt)

            if not image_data:
                logger.error("❌ [Image] 이미지 데이터 획득 실패")
                return None

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

    async def _call_openrouter_api(self, prompt: str) -> Optional[bytes]:
        """OpenRouter Chat API를 통해 이미지 생성 및 다운로드"""
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "model": self.image_model,
                    "messages": [{"role": "user", "content": prompt}],
                    # 일부 모델은 provider별 옵션이 필요할 수 있음
                }

                headers = {
                    "Authorization": f"Bearer {self.openrouter_api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://trpg-studio.com",
                    "X-Title": "TRPG Studio"
                }

                async with session.post(self.openrouter_api_url, json=payload, headers=headers, timeout=60.0) as response:
                    if response.status != 200:
                        err = await response.text()
                        logger.error(f"❌ [Image] API 오류 ({response.status}): {err}")
                        return None

                    result = await response.json()

                    # 응답에서 이미지 URL 추출 (Markdown 또는 텍스트 URL)
                    if "choices" in result and len(result["choices"]) > 0:
                        content = result["choices"][0]["message"]["content"]

                        # 1. Markdown 이미지 링크 추출 (![alt](url))
                        match = re.search(r'!\[.*?\]\((.*?)\)', content)
                        url = match.group(1) if match else None

                        # 2. 없으면 텍스트 자체가 URL인지 확인
                        if not url and content.startswith("http"):
                            url = content.strip()

                        if url:
                            # 이미지 다운로드
                            async with session.get(url) as img_res:
                                if img_res.status == 200:
                                    return await img_res.read()

                    logger.error("❌ [Image] 응답에서 이미지 URL을 찾을 수 없습니다.")
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
            return await self.s3_client.delete_file(image_url.split("/", 3)[-1])
        except: return False

_image_service: Optional[ImageService] = None
def get_image_service() -> ImageService:
    global _image_service
    if _image_service is None: _image_service = ImageService()
    return _image_service