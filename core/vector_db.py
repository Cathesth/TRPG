"""
비동기 Qdrant Vector DB 클라이언트
FastAPI 비동기 환경에 최적화된 NPC 기억 저장 시스템
"""
import os
import logging
import asyncio
from typing import Optional, List, Dict, Any
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

# [수정] 신버전 SDK 임포트 방식
from google import genai
from google.genai import types

import uuid

logger = logging.getLogger(__name__)


class VectorDBClient:
    """비동기 Qdrant 클라이언트 - NPC 기억 및 대화 기록 저장"""

    def __init__(self):
        qdrant_url_raw = os.getenv("QDRANT_URL")

        # 1. Qdrant URL 설정 (HTTPS -> HTTP 변환 등)
        if qdrant_url_raw:
            if qdrant_url_raw.startswith("https://"):
                self.qdrant_url = qdrant_url_raw.replace("https://", "http://")
            elif not qdrant_url_raw.startswith("http://"):
                self.qdrant_url = f"http://{qdrant_url_raw}"
            else:
                self.qdrant_url = qdrant_url_raw

            if ":6333" not in self.qdrant_url and not self.qdrant_url.endswith(":6333"):
                self.qdrant_url = self.qdrant_url.rstrip("/") + ":6333"

            logger.info(f"🔧 [Qdrant] Endpoint URL configured: {self.qdrant_url}")
        else:
            self.qdrant_url = None

        self.qdrant_api_key = os.getenv("QDRANT_API_KEY")
        self.collection_name = os.getenv("QDRANT_COLLECTION", "npc_memories")
        self.vector_size = 768

        # 2. Qdrant 클라이언트 초기화
        self._is_configured = bool(self.qdrant_url)

        if not self._is_configured:
            logger.warning("⚠️ [Qdrant] QDRANT_URL이 설정되지 않았습니다.")
            self.client = None
        else:
            try:
                self.client = AsyncQdrantClient(
                    url=self.qdrant_url,
                    api_key=self.qdrant_api_key,
                    timeout=30,
                    https=False,
                    prefer_grpc=False
                )
                logger.info(f"✅ [Qdrant] Vector DB 클라이언트 초기화 완료")
            except Exception as e:
                logger.error(f"❌ [Qdrant] 초기화 실패: {e}")
                self.client = None
                self._is_configured = False

        # 3. Google GenAI 클라이언트 초기화
        self.google_api_key = os.getenv("GOOGLE_API_KEY")
        self.genai_client = None # [중요] 변수 선언
        self.genai_initialized = False

        if self.google_api_key:
            try:
                self.genai_client = genai.Client(api_key=self.google_api_key)
                self.genai_initialized = True
                logger.info("✅ [Qdrant] Google GenAI 클라이언트 초기화 완료 (text-embedding-004)")
            except Exception as e:
                logger.error(f"❌ [Qdrant] Google GenAI 초기화 실패: {e}")
                self.genai_client = None
        else:
            logger.warning("⚠️ [Qdrant] GOOGLE_API_KEY가 없어 임베딩 생성이 제한됩니다.")

        self._initialized = False

    @property
    def is_available(self) -> bool:
        return self._is_configured and self.client is not None

    async def initialize(self):
        if not self.is_available: return
        if self._initialized: return
        try:
            await self.init_collection()
            self._initialized = True
            logger.info(f"✅ [Qdrant] 컬렉션 '{self.collection_name}' 초기화 완료")
        except Exception as e:
            logger.error(f"❌ [Qdrant] 초기화 중 오류: {e}")
            self._is_configured = False

    async def init_collection(self):
        if not self.is_available: return
        try:
            collections = await self.client.get_collections()
            collection_names = [col.name for col in collections.collections]

            if self.collection_name not in collection_names:
                await self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=VectorParams(size=self.vector_size, distance=Distance.COSINE)
                )
        except Exception as e:
            logger.error(f"❌ [Qdrant] 컬렉션 확인/생성 실패: {e}")

    async def get_gemini_embedding(self, text: str) -> Optional[List[float]]:
        if not self.genai_client:
            return None
        try:
            def _sync_embed():
                response = self.genai_client.models.embed_content(
                    model="text-embedding-004",
                    contents=text,
                    config=types.EmbedContentConfig(task_type="RETRIEVAL_QUERY")
                )
                return response.embeddings[0].values

            embedding = await asyncio.to_thread(_sync_embed)
            return embedding
        except Exception as e:
            logger.error(f"❌ [Qdrant] 임베딩 생성 실패: {e}")
            return None

    async def get_embedding(self, text: str) -> Optional[List[float]]:
        return await self.get_gemini_embedding(text)

    async def upsert_memory(self, npc_id: int, scenario_id: int, text: str, metadata: Optional[Dict[str, Any]] = None) -> bool:
        if not self.is_available: return False
        try:
            vector = await self.get_gemini_embedding(text)
            if not vector: return False

            payload = {"npc_id": npc_id, "scenario_id": scenario_id, "text": text, **(metadata or {})}
            await self.client.upsert(
                collection_name=self.collection_name,
                points=[PointStruct(id=str(uuid.uuid4()), vector=vector, payload=payload)]
            )
            return True
        except Exception as e:
            logger.error(f"❌ [Qdrant] 저장 실패: {e}")
            return False

    async def search_memory(self, query: str, npc_id: Optional[int] = None, scenario_id: Optional[int] = None, limit: int = 5) -> List[Dict[str, Any]]:
        if not self.is_available: return []
        try:
            query_vector = await self.get_gemini_embedding(query)
            if not query_vector: return []

            must_conditions = []
            if npc_id: must_conditions.append({"key": "npc_id", "match": {"value": npc_id}})
            if scenario_id: must_conditions.append({"key": "scenario_id", "match": {"value": scenario_id}})

            # [수정] 안정적인 search 메서드 사용
            results = await self.client.search(
                collection_name=self.collection_name,
                query_vector=query_vector,
                query_filter={"must": must_conditions} if must_conditions else None,
                limit=limit
            )

            formatted_results = []
            for result in results:
                formatted_results.append({
                    "score": result.score,
                    "text": result.payload.get("text", ""),
                    "metadata": result.payload
                })
            return formatted_results
        except Exception as e:
            logger.error(f"❌ [Qdrant] 검색 실패: {e}")
            return []

    # [중요] chatbot_service.py 호환을 위한 search 메서드
    async def search(self, query: str, k: int = 3) -> List[Dict[str, Any]]:
        """
        RAG용 검색 메서드 (ChatbotService에서 호출)
        """
        if not self.is_available:
            return []
        try:
            query_vector = await self.get_gemini_embedding(query)
            if not query_vector:
                return []

            # [수정] query_points 대신 search 사용 (인자 호환성 문제 해결)
            search_result = await self.client.search(
                collection_name=self.collection_name,
                query_vector=query_vector,
                limit=k
            )

            results = []
            for hit in search_result:
                payload = hit.payload or {}
                content = payload.get("text") or payload.get("content") or str(payload)
                results.append({
                    "page_content": content,
                    "metadata": payload,
                    "score": hit.score
                })
            return results
        except Exception as e:
            logger.error(f"❌ [Qdrant] Search Error: {e}")
            return []

    async def delete_npc_memories(self, npc_id: int) -> bool:
        if not self.is_available: return False
        try:
            await self.client.delete(
                collection_name=self.collection_name,
                points_selector={"filter": {"must": [{"key": "npc_id", "match": {"value": npc_id}}]}}
            )
            return True
        except Exception as e:
            logger.error(f"❌ [Qdrant] 삭제 실패: {e}")
            return False

    async def close(self):
        if self.client:
            await self.client.close()
            logger.info("✅ [Qdrant] Client closed successfully")


# 싱글톤 인스턴스
_vector_db_client: Optional[VectorDBClient] = None

def get_vector_db_client() -> VectorDBClient:
    global _vector_db_client
    if _vector_db_client is None:
        _vector_db_client = VectorDBClient()
    return _vector_db_client