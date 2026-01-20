"""
AI 이미지 생성 서비스 (Dual Engine: Gemini 2.0 Flash + Together AI Flux.1)
1. Gemini: 한글 묘사를 Flux 맞춤형 영어 프롬프트로 최적화
2. Together AI: 최적화된 프롬프트로 Flux.1-schnell 모델을 호출하여 고퀄리티 이미지 생성
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

        # 1. Google API Key (프롬프트 최적화용)
        self.google_key = os.getenv("GOOGLE_API_KEY")

        # 2. Together AI Key (Flux 이미지 생성용)
        self.together_key = os.getenv("TOGETHER_API_KEY")

        # 설정
        self.gemini_model = "gemini-2.0-flash"
        self.flux_model = "black-forest-labs/FLUX.1-schnell"
        self.together_url = "https://api.together.xyz/v1/images/generations"

        if not self.google_key or not self.together_key:
            logger.warning("⚠️ [Image] GOOGLE_API_KEY 또는 TOGETHER_API_KEY가 없습니다. 서비스가 제한될 수 있습니다.")
            self._is_available = False
        else:
            try:
                self.gemini_client = genai.Client(api_key=self.google_key)
                self._is_available = True
                logger.info(f"✅ [Image] 하이브리드 엔진 초기화 (Brain: Gemini / Painter: Flux.1)")
            except Exception as e:
                logger.error(f"❌ [Image] 초기화 실패: {e}")
                self._is_available = False

    @property
    def is_available(self) -> bool:
        return self._is_available and self.s3_client.is_available

    async def _optimize_prompt(self, user_description: str, image_type: str) -> str:
        """
        [1단계] Gemini를 사용하여 한글 묘사를 Flux용 영어 프롬프트로 변환
        """
        try:
            # 스타일 가이드 정의
            style_guide = ""
            if image_type == "npc" or image_type == "enemy":
                style_guide = "Style: High quality 8-bit pixel art character sprite, isolated on white background, clean lines, retro RPG aesthetic."
            elif image_type == "background":
                style_guide = "Style: High quality 8-bit pixel art landscape, detailed environment, atmospheric lighting, retro RPG background, 16:9 aspect ratio."

            # 프롬프트 엔지니어링
            instruction = f"""
            You are a professional prompt engineer for the FLUX.1 image generation model.
            Your task is to translate the user's Korean description into a precise, comma-separated English prompt.
            
            1. Translate the atmosphere, lighting, and specific details accurately.
            2. Add visual keywords to enhance quality (e.g., 'masterpiece', 'best quality', 'sharp focus').
            3. Apply this style strictly: {style_guide}
            
            User's Korean description: "{user_description}"
            
            Output ONLY the final English prompt string. Do not include any explanations.
            """

            # 동기 함수를 비동기로 실행
            response = await asyncio.to_thread(
                self.gemini_client.models.generate_content,
                model=self.gemini_model,
                contents=instruction
            )

            optimized_prompt = response.text.strip()
            logger.info(f"🔄 [Prompt] 한글: {user_description[:20]}... -> 영어: {optimized_prompt[:50]}...")
            return optimized_prompt

        except Exception as e:
            logger.error(f"❌ [Prompt] 최적화 실패 (원문 사용): {e}")
            return f"{style_guide} {user_description}"

    async def generate_image(self, image_type: str, description: str, scenario_id: Optional[int] = None, target_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if not self.is_available:
            return None

        try:
            # 1. 프롬프트 최적화 (Gemini)
            final_prompt = await self._optimize_prompt(description, image_type)

            # 2. 이미지 생성 (Flux via Together AI)
            logger.info(f"🎨 [Image] Flux 생성 시작...")
            image_data = await self._call_flux_api(final_prompt)

            if not image_data:
                return None

            # 3. S3 업로드
            image_url = await self._upload_to_s3(image_data, image_type, scenario_id, target_id)

            if not image_url:
                return None

            return {
                "success": True,
                "image_url": image_url,
                "image_type": image_type,
                "description": description,
                "english_prompt": final_prompt, # 디버깅용 저장
                "generated_at": datetime.now().isoformat()
            }
        except Exception as e:
            logger.error(f"❌ [Image] 전체 프로세스 오류: {e}")
            return None

    async def _call_flux_api(self, prompt: str) -> Optional[bytes]:
        """Together AI를 통해 Flux.1-schnell 호출"""
        headers = {
            "Authorization": f"Bearer {self.together_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": self.flux_model,
            "prompt": prompt,
            "width": 1024,
            "height": 1024, # 1:1 비율 (Flux가 가장 안정적)
            "steps": 4,     # Schnell 모델은 4스텝이면 충분
            "n": 1,
            "response_format": "base64"
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.together_url, headers=headers, json=payload, timeout=30.0) as response:
                    if response.status != 200:
                        err = await response.text()
                        logger.error(f"❌ [Flux] API 오류 ({response.status}): {err}")
                        return None

                    result = await response.json()
                    b64_data = result['data'][0]['b64_json']
                    return base64.b64decode(b64_data)

        except Exception as e:
            logger.error(f"❌ [Flux] 연결 실패: {e}")
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