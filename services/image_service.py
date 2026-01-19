"""
AI 이미지 생성 서비스 (OpenRouter 모델 - Image API 호환)
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
        # [수정] 이미지 생성 전용 엔드포인트
        self.openrouter_api_url = "https://openrouter.ai/api/v1/images/generations"
        self.openrouter_api_key = os.getenv("OPENROUTER_API_KEY")
        # [수정] 안정적인 이미지 모델 (SDXL)
        self.image_model = os.getenv("OPENROUTER_IMAGE_MODEL", "stabilityai/stable-diffusion-xl-base-1.0")

        # 이미지 생성 프롬프트 템플릿
        self.prompts = {
            "npc": "8bit pixel art portrait of {description}, game character sprite, retro gaming style, clean lines, vibrant colors, transparent background, centered, high quality",
            "enemy": "8bit pixel art monster of {description}, enemy sprite, retro gaming style, intimidating, clean pixel art, vibrant colors, transparent background, high quality",
            "background": "8bit pixel art landscape of {description}, game background, retro gaming style, detailed environment, atmospheric, vibrant colors, 16:9 aspect ratio"
        }

        if not self.openrouter_api_key:
            logger.warning("⚠️ [Image] OPENROUTER_API_KEY가 설정되지 않았습니다. 이미지 생성이 비활성화됩니다.")
            self._is_available = False
        else:
            self._is_available = True
            logger.info(f"✅ [Image] OpenRouter 이미지 서비스 초기화 (Model: {self.image_model})")

    @property
    def is_available(self) -> bool:
        """이미지 생성 서비스 사용 가능 여부"""
        return self._is_available and self.s3_client.is_available

    async def generate_image(
        self,
        image_type: str,
        description: str,
        scenario_id: Optional[int] = None,
        target_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        if not self.is_available:
            logger.error("❌ [Image] 이미지 생성 서비스를 사용할 수 없습니다.")
            return None

        if image_type not in self.prompts:
            logger.error(f"❌ [Image] 지원되지 않는 이미지 타입: {image_type}")
            return None

        try:
            # 프롬프트 생성
            prompt = self.prompts[image_type].format(description=description)

            # OpenRouter API 호출
            image_data = await self._call_openrouter_api(prompt)
            if not image_data:
                return None

            # S3에 이미지 업로드
            image_url = await self._upload_to_s3(
                image_data,
                image_type,
                scenario_id,
                target_id
            )

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
            logger.error(f"❌ [Image] 이미지 생성 실패: {e}")
            return None

    async def _call_openrouter_api(self, prompt: str) -> Optional[bytes]:
        """OpenRouter Image API 호출"""
        try:
            async with aiohttp.ClientSession() as session:
                # [수정] 표준 이미지 생성 Payload
                payload = {
                    "model": self.image_model,
                    "prompt": prompt,
                    "n": 1,
                    "width": 1024,
                    "height": 1024
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
                        logger.error(f"❌ [Image] API 오류: {response.status} - {error_text}")
                        return None

                    result = await response.json()

                    # 응답 처리 (URL 또는 B64_JSON)
                    if "data" in result and len(result["data"]) > 0:
                        image_obj = result["data"][0]

                        if "url" in image_obj:
                            # URL로 이미지가 온 경우 다운로드
                            async with session.get(image_obj["url"]) as img_res:
                                if img_res.status == 200:
                                    return await img_res.read()
                        elif "b64_json" in image_obj:
                            # Base64로 온 경우 디코딩
                            return base64.b64decode(image_obj["b64_json"])

                    logger.error("❌ [Image] API 응답에 이미지 데이터가 없습니다.")
                    return None

        except Exception as e:
            logger.error(f"❌ [Image] API 호출 중 예외 발생: {e}")
            return None

    async def _upload_to_s3(self, image_data: bytes, image_type: str, scenario_id: Optional[int] = None, target_id: Optional[str] = None) -> Optional[str]:
        """S3 업로드 (기존 로직 유지)"""
        try:
            folder_parts = ["ai-images"]
            if scenario_id:
                folder_parts.append(str(scenario_id))
            folder_parts.append(image_type)

            folder = "/".join(folder_parts)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            unique_id = str(uuid.uuid4())[:8]
            filename = f"{target_id or 'generated'}_{timestamp}_{unique_id}.png"

            image_url = await self.s3_client.upload_file(
                file_data=image_data,
                filename=filename,
                content_type="image/png",
                folder=folder
            )

            if image_url:
                logger.info(f"✅ [Image] 이미지 업로드 성공: {image_url}")
                return image_url
            else:
                logger.error("❌ [Image] S3 업로드 실패")
                return None
        except Exception as e:
            logger.error(f"❌ [Image] S3 업로드 오류: {e}")
            return None

    async def delete_image(self, image_url: str) -> bool:
        """S3 이미지 삭제 (기존 로직 유지)"""
        try:
            if not self.s3_client.is_available or "/" not in image_url:
                return False
            s3_key = image_url.split("/", 3)[-1]
            return await self.s3_client.delete_file(s3_key)
        except Exception as e:
            logger.error(f"❌ [Image] 이미지 삭제 실패: {e}")
            return False

# 싱글톤 인스턴스
_image_service: Optional[ImageService] = None

def get_image_service() -> ImageService:
    global _image_service
    if _image_service is None:
        _image_service = ImageService()
    return _image_service