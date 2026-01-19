"""
AI 이미지 생성 서비스 (OpenRouter 모델)
Railway 환경에서 MiniO에 이미지 저장/로드 지원
"""
import os
import logging
import asyncio
import aiohttp
from typing import Optional, Dict, Any
from datetime import datetime
import uuid

from core.s3_client import get_s3_client

logger = logging.getLogger(__name__)


class ImageService:
    """AI 이미지 생성 및 관리 서비스"""
    
    def __init__(self):
        self.s3_client = get_s3_client()
        self.openrouter_api_url = "https://openrouter.ai/api/v1/chat/completions"
        self.openrouter_api_key = os.getenv("OPENROUTER_API_KEY")
        self.image_model = os.getenv("OPENROUTER_IMAGE_MODEL", "nanobanana")
        
        # 이미지 생성 프롬프트 템플릿
        self.prompts = {
            "npc": "Create an 8bit pixel art portrait of {description}, game character sprite, retro gaming style, clean lines, vibrant colors, transparent background. The image should be suitable for a TRPG game character.",
            "enemy": "Create an 8bit pixel art monster of {description}, enemy sprite, retro gaming style, intimidating but not scary, clean pixel art, vibrant colors, transparent background. The image should be suitable for a TRPG game enemy.",
            "background": "Create an 8bit pixel art landscape of {description}, game background, retro gaming style, detailed environment, atmospheric, vibrant colors, 16:9 aspect ratio. The image should be suitable as a TRPG scene background."
        }
        
        if not self.openrouter_api_key:
            logger.warning("⚠️ [Image] OPENROUTER_API_KEY가 설정되지 않았습니다. 이미지 생성이 비활성화됩니다.")
            self._is_available = False
        else:
            self._is_available = True
            logger.info("✅ [Image] OpenRouter 이미지 생성 서비스 초기화 완료")
    
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
        """
        AI 이미지 생성
        
        Args:
            image_type: 이미지 타입 ('npc', 'enemy', 'background')
            description: 생성할 이미지 설명
            scenario_id: 시나리오 ID (폴더 구조용)
            target_id: 대상 ID (NPC/씬 ID)
            
        Returns:
            생성된 이미지 정보 딕셔너리 또는 None
        """
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
        """OpenRouter API 호출하여 이미지 데이터 받기"""
        try:
            async with aiohttp.ClientSession() as session:
                # Use a text-to-image model from OpenRouter
                payload = {
                    "model": self.image_model,
                    "messages": [
                        {
                            "role": "user",
                            "content": prompt
                        }
                    ],
                    "response_format": {
                        "type": "image",
                        "image": {
                            "size": "1024x1024",
                            "quality": "standard"
                        }
                    }
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
                        logger.error(f"❌ [Image] OpenRouter API 오류: {response.status} - {error_text}")
                        return None
                    
                    # 이미지 데이터 추출
                    result = await response.json()
                    if "choices" in result and len(result["choices"]) > 0:
                        # 이미지 URL 또는 base64 데이터 추출
                        choice = result["choices"][0]
                        if "message" in choice and "content" in choice["message"]:
                            content = choice["message"]["content"]
                            if isinstance(content, str) and content.startswith("data:image"):
                                # base64 이미지 디코딩
                                import base64
                                image_base64 = content.split(",")[1]  # data:image/png;base64, 제거
                                return base64.b64decode(image_base64)
                            elif isinstance(content, str) and content.startswith("http"):
                                # URL에서 이미지 다운로드
                                async with session.get(content) as img_response:
                                    if img_response.status == 200:
                                        return await img_response.read()
                                    else:
                                        logger.error(f"❌ [Image] 이미지 다운로드 실패: {img_response.status}")
                                        return None
                        else:
                            logger.error("❌ [Image] OpenRouter API 응답 형식이 잘못되었습니다.")
                            return None
                    else:
                        logger.error("❌ [Image] OpenRouter API 응답에 이미지 데이터가 없습니다.")
                        return None
                        
        except asyncio.TimeoutError:
            logger.error("❌ [Image] OpenRouter API 타임아웃")
            return None
        except Exception as e:
            logger.error(f"❌ [Image] OpenRouter API 호출 실패: {e}")
            return None
    
    async def _upload_to_s3(
        self, 
        image_data: bytes, 
        image_type: str,
        scenario_id: Optional[int] = None,
        target_id: Optional[str] = None
    ) -> Optional[str]:
        """생성된 이미지를 S3(MiniO)에 업로드"""
        try:
            # 폴더 구조 생성: ai-images/{scenario_id}/{image_type}/{target_id}.png
            folder_parts = ["ai-images"]
            if scenario_id:
                folder_parts.append(str(scenario_id))
            folder_parts.append(image_type)
            
            folder = "/".join(folder_parts)
            
            # 파일명 생성
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            unique_id = str(uuid.uuid4())[:8]
            filename = f"{target_id or 'generated'}_{timestamp}_{unique_id}.png"
            
            # S3 업로드
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
            logger.error(f"❌ [Image] S3 업로드 중 오류: {e}")
            return None
    
    async def delete_image(self, image_url: str) -> bool:
        """S3에서 이미지 삭제"""
        try:
            if not self.s3_client.is_available:
                return False
            
            # URL에서 S3 키 추출
            if "/" in image_url:
                s3_key = image_url.split("/", 3)[-1]  # 도메인/버킷/제외하고 나머지
            else:
                return False
            
            return await self.s3_client.delete_file(s3_key)
            
        except Exception as e:
            logger.error(f"❌ [Image] 이미지 삭제 실패: {e}")
            return False


# 싱글톤 인스턴스
_image_service: Optional[ImageService] = None


def get_image_service() -> ImageService:
    """이미지 서비스 싱글톤 인스턴스 반환"""
    global _image_service
    if _image_service is None:
        _image_service = ImageService()
    return _image_service
