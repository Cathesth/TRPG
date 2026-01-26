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

# [수정] 신버전 SDK 임포트 방식 (google-genai 패키지 사용 시)
from google import genai
from google.genai import types

import uuid

logger = logging.getLogger(__name__)


class VectorDBClient:
    """비동기 Qdrant 클라이언트 - NPC 기억 및 대화 기록 저장"""

    def __init__(self):
        qdrant_url_raw = os.getenv("QDRANT_URL")

        # ✅ [작업 2] HTTPS를 HTTP로 강제 치환 및 포트 보정 (내부망 SSL 문제 해결)
        if qdrant_url_raw:
            # 1. HTTPS를 HTTP로 변환
            if qdrant_url_raw.startswith("https://"):
                self.qdrant_url = qdrant_url_raw.replace("https://", "http://")
            # 2. HTTP 프로토콜이 없으면 http:// 추가
            elif not qdrant_url_raw.startswith("http://"):
                self.qdrant_url = f"http://{qdrant_url_raw}"
            else:
                self.qdrant_url = qdrant_url_raw

            # 포트 번호가 없으면 :6333 추가
            if ":6333" not in self.qdrant_url and not self.qdrant_url.endswith(":6333"):
                # URL 끝에 슬래시가 있으면 제거 후 포트 추가
                self.qdrant_url = self.qdrant_url.rstrip("/") + ":6333"

            logger.info(f"🔧 [Qdrant] Endpoint URL configured: {self.qdrant_url}")
        else:
            self.qdrant_url = None

        self.qdrant_api_key = os.getenv("QDRANT_API_KEY")
        self.collection_name = os.getenv("QDRANT_COLLECTION", "npc_memories")
        self.vector_size = 768  # Google Gemini text-embedding-004 차원


        # [수정] 로컬 환경 배려: Qdrant URL 확인 로직 위치 조정
        # 로컬 환경 배려: Qdrant URL이 없으면 비활성화
        self._is_configured = bool(self.qdrant_url)

        # [수정 후] 비동기(Async) 클라이언트 및 옵션 적용
        if not self._is_configured:
            logger.warning("⚠️ [Qdrant] QDRANT_URL이 설정되지 않았습니다. Vector DB 기능이 비활성화됩니다.")
            self.client = None
        else:
            try:
                # ✅ [핵심 변경] AsyncQdrantClient 사용, https=False, prefer_grpc=False 설정
                self.client = AsyncQdrantClient(
                    url=self.qdrant_url,
                    api_key=self.qdrant_api_key,
                    timeout=30,
                    https=False,  # SSL 비활성화 (내부망 통신 등 문제 해결)
                    prefer_grpc=False  # REST API 강제 사용
                )
                logger.info(f"✅ [Qdrant] Vector DB 클라이언트 초기화 완료: {self.qdrant_url}")
            except Exception as e:
                logger.error(f"❌ [Qdrant] 초기화 실패: {e}")
                self.client = None
                self._is_configured = False

        # ▼▼▼ [추가해야 할 부분] ▼▼▼
        # ✅ [작업 1] Google GenAI 클라이언트 초기화 (신버전 SDK)
        self.google_api_key = os.getenv("GOOGLE_API_KEY")
        self.genai_client = None
        self.genai_initialized = False  # 호환성을 위한 플래그 (선택)

        if self.google_api_key:
            try:
                # genai.Client 인스턴스 생성
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
        """Vector DB 기능이 사용 가능한지 확인"""
        return self._is_configured and self.client is not None

    async def initialize(self):
        """앱 시작 시 컬렉션 초기화 (없으면 생성)"""
        if not self.is_available:
            logger.warning("⚠️ [Qdrant] Vector DB가 구성되지 않아 초기화를 건너뜁니다.")
            return

        if self._initialized:
            return

        try:
            await self.init_collection()
            self._initialized = True
            logger.info(f"✅ [Qdrant] 컬렉션 '{self.collection_name}' 초기화 완료")
        except Exception as e:
            logger.error(f"❌ [Qdrant] 초기화 중 오류: {e}")
            self._is_configured = False

    async def init_collection(self):
        """컬렉션 생성 (존재하지 않을 경우)"""
        if not self.is_available:
            return

        try:
            # 기존 컬렉션 확인
            collections = await self.client.get_collections()
            collection_names = [col.name for col in collections.collections]

            if self.collection_name not in collection_names:
                # 컬렉션 생성
                await self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=VectorParams(
                        size=self.vector_size,
                        distance=Distance.COSINE
                    )
                )
                logger.info(f"📦 [Qdrant] 새 컬렉션 생성: {self.collection_name}")
            else:
                logger.info(f"📦 [Qdrant] 기존 컬렉션 사용: {self.collection_name}")

        except Exception as e:
            logger.error(f"❌ [Qdrant] 컬렉션 초기화 실패: {e}")
            raise

    async def get_gemini_embedding(self, text: str) -> Optional[List[float]]:
        """
        ✅ [수정] Google GenAI 신버전 SDK 사용 (models.embed_content)
        """
        if not self.genai_client:
            logger.warning("⚠️ [Qdrant] Google GenAI 클라이언트가 초기화되지 않았습니다.")
            return None

        try:
            # [수정] 동기 함수 래핑 (신버전 SDK 문법 적용)
            def _sync_embed():
                response = self.genai_client.models.embed_content(
                    model="text-embedding-004",
                    contents=text,
                    config=types.EmbedContentConfig(task_type="RETRIEVAL_QUERY")
                )
                # 신버전 응답 구조에서 임베딩 추출
                return response.embeddings[0].values

            embedding = await asyncio.to_thread(_sync_embed)
            return embedding

        except Exception as e:
            logger.error(f"❌ [Qdrant] Google GenAI 임베딩 생성 실패: {e}")
            return None

    async def get_embedding(self, text: str) -> Optional[List[float]]:
        return await self.get_gemini_embedding(text)

    async def upsert_memory(self, npc_id: int, scenario_id: int, text: str, metadata: Optional[Dict[str, Any]] = None) -> bool:
        if not self.is_available:
            return False
        try:
            vector = await self.get_gemini_embedding(text)
            if not vector:
                return False

            payload = {
                "npc_id": npc_id,
                "scenario_id": scenario_id,
                "text": text,
                **(metadata or {})
            }
            point_id = str(uuid.uuid4())
            await self.client.upsert(
                collection_name=self.collection_name,
                points=[
                    PointStruct(
                        id=point_id,
                        vector=vector,
                        payload=payload
                    )
                ]
            )
            logger.info(f"💾 [Qdrant] 기억 저장 완료: NPC={npc_id}")
            return True
        except Exception as e:
            logger.error(f"❌ [Qdrant] 기억 저장 실패: {e}")
            return False

    async def search_memory(self, query: str, npc_id: Optional[int] = None, scenario_id: Optional[int] = None, limit: int = 5) -> List[Dict[str, Any]]:
        if not self.is_available:
            return []
        try:
            query_vector = await self.get_gemini_embedding(query)
            if not query_vector:
                return []

            query_filter = None
            if npc_id or scenario_id:
                must_conditions = []
                if npc_id:
                    must_conditions.append({"key": "npc_id", "match": {"value": npc_id}})
                if scenario_id:
                    must_conditions.append({"key": "scenario_id", "match": {"value": scenario_id}})
                query_filter = {"must": must_conditions}

            response = await self.client.query_points(
                collection_name=self.collection_name,
                query=query_vector,  # 매개변수명: query_vector -> query
                filter=query_filter,  # 매개변수명: query_filter -> filter
                limit=limit
            )
            results = response.points  # 결과 객체에서 points 리스트 추출

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
        if not self.is_available:
            return []
        try:
            query_vector = await self.get_gemini_embedding(query)
            if not query_vector:
                return []

            response = await self.client.query_points(
                collection_name=self.collection_name,
                query=query_vector,  # query_vector -> query
                limit=k
            )
            search_result = response.points  # 결과 추출

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
        if not self.is_available:
            return False
        try:
            await self.client.delete(
                collection_name=self.collection_name,
                points_selector={"filter": {"must": [{"key": "npc_id", "match": {"value": npc_id}}]}}
            )
            return True
        except Exception as e:
            logger.error(f"❌ [Qdrant] 삭제 실패: {e}")
            return False

    # ▼▼▼ [여기] close 메서드 추가 ▼▼▼
    async def close(self):
        """Qdrant 클라이언트 연결 종료"""
        if self.client:
            await self.client.close()
            logger.info("✅ [Qdrant] Client closed successfully")

# 싱글톤 인스턴스
_vector_db_client: Optional[VectorDBClient] = None


def get_vector_db_client() -> VectorDBClient:
    """Vector DB 클라이언트 싱글톤 인스턴스 반환"""
    global _vector_db_client
    if _vector_db_client is None:
        _vector_db_client = VectorDBClient()
    return _vector_db_client
