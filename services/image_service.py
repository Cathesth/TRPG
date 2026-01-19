"""
AI 이미지 생성 서비스 (OpenRouter Chat API 호환 - Universal)
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
        # [중요] 모든 모델에 대해 Chat API 사용
        self.openrouter_api_url = "https://openrouter.ai/api/v1/chat/completions"
        self.openrouter_api_key = os.getenv("OPENROUTER_API_KEY")

        # [권장] 호환성이 가장 좋은 DALL-E 3를 기본값으로 사용하거나,
        # Flux 사용 시에는 'black-forest-labs/flux-1-schnell' 설정
        self.image_model = os.getenv("OPENROUTER_IMAGE_MODEL", "openai/dall-e-3")

        # 이미지 생성 프롬프트 템플릿
        # (URL을 반환하도록 명시적으로 요청)
        self.prompts = {
            "npc": "Generate an image of {description}. 8bit pixel art portrait, game character sprite, retro gaming style, white background, centered. Return ONLY the image URL or the image itself.",
            "enemy": "Generate an image of {description}. 8bit pixel art monster, enemy sprite, retro gaming style, intimidating, white background. Return ONLY the image URL or the image itself.",
            "background": "Generate an image of {description}. 8bit pixel art landscape, game background, retro gaming style, detailed environment, 16:9 aspect ratio. Return ONLY the image URL or the image itself."
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
                # [수정] 복잡한 파라미터 제거 (Flux 등 호환성 확보)
                payload = {
                    "model": self.image_model,
                    "messages": [{"role": "user", "content": prompt}]
                }

                # DALL-E 3인 경우에만 response_format 추가
                if "dall-e-3" in self.image_model:
                    payload["response_format"] = {"type": "url"} # DALL-E 3는 url 지원

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

                    # 1. content 내의 텍스트에서 URL 추출 (Flux, Gemini 등)
                    if "choices" in result and len(result["choices"]) > 0:
                        message = result["choices"][0]["message"]
                        content = message.get("content", "")

                        # Markdown 이미지 링크 (![alt](url)) 찾기
                        match = re.search(r'!\[.*?\]\((https?://[^\)]+)\)', content)
                        url = match.group(1) if match else None

                        # Markdown 링크 없으면 일반 URL 찾기
                        if not url:
                            match_url = re.search(r'https?://[^\s<>"]+', content)
                            if match_url:
                                url = match_url.group(0)

                        # DALL-E 스타일 (message.content가 아니라 tool_calls나 별도 필드일 수도 있지만 OpenRouter는 보통 content에 줌)
                        # 일부 모델은 result['data'][0]['url'] 형식을 따르기도 함 (OpenAI 포맷)
                        if not url and "data" in result and isinstance(result["data"], list):
                             if "url" in result["data"][0]:
                                 url = result["data"][0]["url"]

                        if url:
                            logger.info(f"✅ [Image] 이미지 URL 발견: {url[:30]}...")
                            # 이미지 다운로드
                            async with session.get(url) as img_res:
                                if img_res.status == 200:
                                    return await img_res.read()
                                else:
                                    logger.error(f"❌ [Image] URL 다운로드 실패: {img_res.status}")

                        # Base64 처리 (DALL-E 등)
                        # 만약 content 자체가 base64라면? (드묾)

                    logger.error(f"❌ [Image] 응답에서 이미지를 찾을 수 없음. 응답 요약: {str(result)[:100]}...")
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