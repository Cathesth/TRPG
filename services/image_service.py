"""
AI 이미지 생성 서비스 (Hugging Face API 기반 - FLUX.1)
Railway 환경에서 MiniO에 이미지 저장/로드 지원
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
        # [설정] Railway 환경변수에 HF_TOKEN을 꼭 추가해야 합니다.
        self.hf_token = os.getenv("HF_TOKEN")

        # [모델] Hugging Face의 최신 고속 모델 (FLUX.1-schnell)
        # 무료 Inference API를 통해 호출합니다.
        self.api_url = "https://api-inference.huggingface.co/models/black-forest-labs/FLUX.1-schnell"

        # 프롬프트 템플릿 (Flux 모델은 자연어 지시를 잘 알아듣습니다)
        self.prompts = {
            "npc": "pixel art portrait of {description}, 8-bit style, retro rpg character, white background, centered, high quality, sharp focus, clean lines, minimal details",
            "enemy": "pixel art monster of {description}, 8-bit style, retro rpg enemy, white background, intimidating, high quality, clean lines",
            "background": "pixel art landscape of {description}, 8-bit style, retro rpg background, detailed environment, atmospheric, 16:9 aspect ratio"
        }

        if not self.hf_token:
            logger.warning("⚠️ [Image] HF_TOKEN(Hugging Face 토큰)이 없습니다. 이미지 생성이 실패할 수 있습니다.")
            self._is_available = False
        else:
            self._is_available = True
            logger.info(f"✅ [Image] Hugging Face 서비스 초기화 (Model: FLUX.1-schnell)")

    @property
    def is_available(self) -> bool:
        return self._is_available and self.s3_client.is_available

    async def generate_image(self, image_type: str, description: str, scenario_id: Optional[int] = None, target_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if not self.is_available or image_type not in self.prompts:
            return None

        try:
            # 프롬프트 생성
            prompt = self.prompts[image_type].format(description=description)
            logger.info(f"🎨 [Image] 생성 요청: {prompt[:50]}...")

            # API 호출
            image_data = await self._call_huggingface_api(prompt)

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
            logger.error(f"❌ [Image] 생성 프로세스 오류: {e}")
            return None

    async def _call_huggingface_api(self, prompt: str) -> Optional[bytes]:
        """Hugging Face Inference API 호출"""
        try:
            headers = {"Authorization": f"Bearer {self.hf_token}"}
            payload = {
                "inputs": prompt,
                "parameters": {
                    # 필요시 파라미터 조정 가능
                    # "guidance_scale": 3.5,
                    # "num_inference_steps": 4
                }
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url, headers=headers, json=payload, timeout=60.0) as response:
                    if response.status != 200:
                        err = await response.text()
                        logger.error(f"❌ [Image] API 오류 ({response.status}): {err}")

                        # 503(모델 로딩중) 에러 발생 시 처리 로직이 필요할 수 있음
                        if response.status == 503:
                            logger.info("⏳ [Image] 모델 로딩 중... 잠시 후 다시 시도해주세요.")

                        return None

                    # 이미지가 바이너리 형태로 반환됨
                    logger.info("✅ [Image] 이미지 데이터 수신 성공")
                    return await response.read()

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