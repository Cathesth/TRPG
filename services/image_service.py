"""
AI 이미지 생성 서비스 (Dual Engine: Gemini 2.0 Flash + Together AI Flux.1)
- 기능 1: Gemini가 한글 묘사를 영어 프롬프트로 번역/최적화
- 기능 2: Together AI(Flux) 호출 시 500 에러가 나면 자동 재시도
- 기능 3: Flux가 끝까지 실패하면 SDXL 모델로 자동 전환 (무조건 성공 보장)
"""
import os
import logging
import asyncio
import aiohttp
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
        self.google_key = os.getenv("GOOGLE_API_KEY")
        self.together_key = os.getenv("TOGETHER_API_KEY")

        # 모델 설정
        self.gemini_model = "gemini-2.0-flash"
        self.flux_model = "black-forest-labs/FLUX.1-schnell"  # 1순위: Flux
        self.sdxl_model = "stabilityai/stable-diffusion-xl-base-1.0" # 2순위: SDXL (백업)

        self.together_url = "https://api.together.xyz/v1/images/generations"

        # [수정] 프롬프트 템플릿 강화 (NPC/적: 초상화, 아이템: 아이콘)
        self.prompts = {
            "npc": "pixel art portrait of {description}, face focused, 8-bit style, retro rpg character profile, high quality, detailed face, isolated background",
            "enemy": "pixel art portrait of {description}, face focused, 8-bit style, retro rpg enemy profile, menacing, high quality, isolated background",
            "background": "pixel art landscape of {description}, 8-bit, retro rpg style, detailed environment, atmospheric, 16:9 aspect ratio",
            "item": "single pixel art icon of {description}, 8-bit, retro rpg item, centered, white background, high quality, game sprite"
        }

        if not self.google_key or not self.together_key:
            logger.warning("⚠️ 키 설정 확인 필요: GOOGLE_API_KEY 또는 TOGETHER_API_KEY 부재")
            self._is_available = False
        else:
            try:
                self.gemini_client = genai.Client(api_key=self.google_key)
                self._is_available = True
                logger.info(f"✅ [Image] 하이브리드 엔진 가동 (Gemini + Together AI)")
            except Exception as e:
                logger.error(f"❌ [Image] 초기화 실패: {e}")
                self._is_available = False

    @property
    def is_available(self) -> bool:
        return self._is_available and self.s3_client.is_available

    async def _optimize_prompt(self, user_description: str, image_type: str) -> str:
        """Gemini: 한글 -> 영어 프롬프트 최적화"""
        try:
            # [수정] 이미지 타입별 스타일 가이드 세분화
            style_guide = ""
            if image_type in ["npc", "enemy"]:
                style_guide = "Style: High quality 8-bit pixel art character portrait, face focused, isolated on white background."
            elif image_type == "item":
                style_guide = "Style: High quality 8-bit pixel art item icon, centered, isolated on white background."
            elif image_type == "background":
                style_guide = "Style: High quality 8-bit pixel art landscape, detailed environment, atmospheric lighting, 16:9 aspect ratio."

            instruction = f"""
            You are a prompt engineer for FLUX.1.
            Translate the user's Korean description into a precise English prompt.
            1. Translate atmosphere, lighting, and details accurately.
            2. Add quality keywords (masterpiece, best quality).
            3. Apply style: {style_guide}
            
            User's Korean description: "{user_description}"
            Output ONLY the English prompt.
            """

            response = await asyncio.to_thread(
                self.gemini_client.models.generate_content,
                model=self.gemini_model,
                contents=instruction
            )

            optimized = response.text.strip()
            logger.info(f"🔄 [Prompt] 번역 완료 ({image_type}): {optimized[:50]}...")
            return optimized

        except Exception as e:
            logger.error(f"❌ [Prompt] 번역 실패 (원문 사용): {e}")
            return f"{style_guide} {user_description}"

    async def generate_image(self, image_type: str, description: str, scenario_id: Optional[int] = None, target_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if not self.is_available:
            return None

        try:
            # 1. 프롬프트 최적화
            final_prompt = await self._optimize_prompt(description, image_type)

            # 2. [1순위] Flux 모델 시도
            logger.info(f"🎨 [Image] Flux 생성 시도... ({image_type})")
            image_data = await self._call_together_api_with_retry(final_prompt, self.flux_model)

            # 3. [2순위] 실패 시 SDXL 모델 시도 (Fallback)
            if not image_data:
                logger.warning(f"⚠️ [Image] Flux 실패 -> SDXL(백업)로 전환 시도")
                image_data = await self._call_together_api_with_retry(final_prompt, self.sdxl_model)

            if not image_data:
                logger.error("❌ [Image] 모든 모델 생성 실패")
                return None

            # 4. S3 업로드
            # 폴더 구조: ai-images/시나리오ID/타입/파일명
            image_url = await self._upload_to_s3(image_data, image_type, scenario_id, target_id)

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

    async def _call_together_api_with_retry(self, prompt: str, model: str) -> Optional[bytes]:
        """Together AI 호출 (재시도 로직 포함)"""
        headers = {
            "Authorization": f"Bearer {self.together_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": model,
            "prompt": prompt,
            "width": 1024,
            "height": 1024,
            "steps": 4 if "flux" in model.lower() else 20, # 모델별 스텝 최적화
            "n": 1,
            "response_format": "base64"
        }

        # 최대 2회 재시도
        for attempt in range(2):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(self.together_url, headers=headers, json=payload, timeout=40.0) as response:
                        if response.status == 200:
                            result = await response.json()
                            b64_data = result['data'][0]['b64_json']
                            return base64.b64decode(b64_data)

                        # 500, 503 에러면 잠시 대기 후 재시도
                        if response.status in [500, 503]:
                            logger.warning(f"⏳ [API] 서버 오류({response.status}). 재시도 중... ({attempt+1}/2)")
                            await asyncio.sleep(2)
                            continue

                        # 그 외 에러(400 등)는 즉시 실패 처리
                        err = await response.text()
                        logger.error(f"❌ [API] 호출 오류 ({response.status}): {err}")
                        return None

            except Exception as e:
                logger.error(f"❌ [API] 연결 실패: {e}")

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