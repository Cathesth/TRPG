"""
AI 이미지 생성 서비스 (OpenRouter Image API 호환)
Railway 환경에서 MiniO에 이미지 저장/로드 지원
"""
import os
import logging
import asyncio
import aiohttp
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
        # [수정] Flux 모델은 이미지 전용 엔드포인트를 사용해야 합니다.
        self.openrouter_api_url = "https://openrouter.ai/api/v1/images/generations"
        self.openrouter_api_key = os.getenv("OPENROUTER_API_KEY")

        # [수정] OpenRouter에서 가장 안정적인 최신 이미지 모델 (Flux 1 Schnell)
        # 만약 이 모델도 안 되면 'openai/dall-e-3' 등을 시도해볼 수 있습니다.
        self.image_model = os.getenv("OPENROUTER_IMAGE_MODEL", "black-forest-labs/flux-1-schnell")

        # 이미지 생성 프롬프트 템플릿
        self.prompts = {
            "npc": "8bit pixel art portrait of {description}, game character sprite, retro gaming style, white background, centered, high quality",
            "enemy": "8bit pixel art monster of {description}, enemy sprite, retro gaming style, intimidating, white background, high quality",
            "background": "8bit pixel art landscape of {description}, game background, retro gaming style, detailed environment, atmospheric, 16:9 aspect ratio"
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
        """OpenRouter Image API 호출"""
        try:
            async with aiohttp.ClientSession() as session:
                # [중요] OpenAI 호환 이미지 생성 Payload
                payload = {
                    "model": self.image_model,
                    "prompt": prompt,
                    "n": 1,
                    # Flux 모델은 size 대신 width/height를 명시하는 것이 안전할 수 있음
                    # 하지만 OpenAI 호환성을 위해 size 문자열도 지원하는 경우가 많음.
                    # 우선 표준 "1024x1024"를 사용.
                    "size": "1024x1024"
                }

                headers = {
                    "Authorization": f"Bearer {self.openrouter_api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://trpg-studio.com",
                    "X-Title": "TRPG Studio"
                }

                async with session.post(
                    self.openrouter_api_url,
                    json=payload,
                    headers=headers,
                    timeout=60.0
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"❌ [Image] API 오류 ({response.status}): {error_text}")
                        return None

                    result = await response.json()

                    # 결과 파싱 (URL 또는 Base64)
                    if "data" in result and len(result["data"]) > 0:
                        image_obj = result["data"][0]

                        if "url" in image_obj:
                            async with session.get(image_obj["url"]) as img_res:
                                if img_res.status == 200:
                                    return await img_res.read()
                        elif "b64_json" in image_obj:
                            return base64.b64decode(image_obj["b64_json"])

                    logger.error("❌ [Image] 응답에 이미지 데이터가 없음")
                    return None

        except Exception as e:
            logger.error(f"❌ [Image] API 호출 실패: {e}")
            return None

    async def _upload_to_s3(self, image_data: bytes, image_type: str, scenario_id: Optional[int] = None, target_id: Optional[str] = None) -> Optional[str]:
        try:
            folder = f"ai-images/{scenario_id}/{image_type}" if scenario_id else f"ai-images/{image_type}"
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            unique_id = str(uuid.uuid4())[:8]
            filename = f"{target_id or 'generated'}_{timestamp}_{unique_id}.png"

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
    if _image_service is None:
        _image_service = ImageService()
    return _image_service