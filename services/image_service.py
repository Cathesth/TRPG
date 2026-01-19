"""
AI 이미지 생성 서비스 (OpenRouter Chat API - Modalities 호환)
Railway 환경에서 MiniO에 이미지 저장/로드 지원
"""
import os
import logging
import asyncio
import aiohttp
import re
import base64
from typing import Optional, Dict, Any
from datetime import datetime
import uuid

from core.s3_client import get_s3_client

logger = logging.getLogger(__name__)

class ImageService:
    """AI 이미지 생성 및 관리 서비스"""

    def __init__(self):
        self.s3_client = get_s3_client()
        # [중요] OpenRouter Chat API 사용
        self.openrouter_api_url = "https://openrouter.ai/api/v1/chat/completions"
        self.openrouter_api_key = os.getenv("OPENROUTER_API_KEY")

        # [수정] OpenRouter에서 확실히 작동하는 무료/저가형 모델 (Gemini 2.0 Flash Exp)
        # Dall-E 3는 OpenRouter에서 지원하지 않습니다.
        self.image_model = os.getenv("OPENROUTER_IMAGE_MODEL", "google/gemini-2.0-flash-exp")

        # 이미지 생성 프롬프트 템플릿
        self.prompts = {
            "npc": "Draw an 8bit pixel art portrait of {description}, game character sprite, retro gaming style, white background, centered. Return ONLY the image.",
            "enemy": "Draw an 8bit pixel art monster of {description}, enemy sprite, retro gaming style, intimidating, white background. Return ONLY the image.",
            "background": "Draw an 8bit pixel art landscape of {description}, game background, retro gaming style, detailed environment, 16:9 aspect ratio. Return ONLY the image."
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
                logger.error("❌ [Image] 이미지 데이터를 받아오지 못했습니다.")
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
            logger.error(f"❌ [Image] 생성 중 예외 발생: {e}")
            return None

    async def _call_openrouter_api(self, prompt: str) -> Optional[bytes]:
        """OpenRouter Chat API를 통해 이미지 생성 및 다운로드"""
        try:
            async with aiohttp.ClientSession() as session:
                # [핵심 수정] modalities 파라미터 추가 (이미지 생성 트리거)
                payload = {
                    "model": self.image_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "modalities": ["image", "text"]
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

                    # 응답 처리 로직
                    if "choices" in result and len(result["choices"]) > 0:
                        message = result["choices"][0]["message"]

                        # 1. OpenRouter 전용 'images' 필드 확인 (일부 모델)
                        if "images" in message and message["images"]:
                            # 보통 URL이나 Base64가 리스트로 옴
                            img_content = message["images"][0]
                            if img_content.startswith("http"):
                                async with session.get(img_content) as img_res:
                                    if img_res.status == 200: return await img_res.read()
                            else:
                                return base64.b64decode(img_content)

                        # 2. Markdown 이미지 링크 추출 (![alt](url))
                        content = message.get("content", "")
                        match = re.search(r'!\[.*?\]\((https?://[^\)]+)\)', content)
                        url = match.group(1) if match else None

                        # 3. 텍스트 내 일반 URL 추출
                        if not url:
                            match_url = re.search(r'https?://[^\s<>"]+', content)
                            if match_url:
                                url = match_url.group(0)

                        if url:
                            logger.info(f"✅ [Image] 이미지 URL 발견: {url[:30]}...")
                            async with session.get(url) as img_res:
                                if img_res.status == 200:
                                    return await img_res.read()
                                else:
                                    logger.error(f"❌ [Image] URL 다운로드 실패: {img_res.status}")

                    logger.error(f"❌ [Image] 응답에서 이미지를 찾을 수 없음.")
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