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
from google import genai
import uuid

logger = logging.getLogger(__name__)


class VectorDBClient:
    """비동기 Qdrant 클라이언트 - NPC 기억 및 대화 기록 저장"""

    def __init__(self):
        qdrant_url_raw = os.getenv("QDRANT_URL")

        # ✅ [작업 2] HTTPS를 HTTP로 강제 치환 및 포트 보정 (내부망 SSL 문제 해결)
        if qdrant_url_raw:
            # HTTPS를 HTTP로 변환
            if qdrant_url_raw.startswith("https://"):
                self.qdrant_url = qdrant_url_raw.replace("https://", "http://")
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

        # ✅ [작업 1] Google GenAI 최신 라이브러리 클라이언트
        self.google_api_key = os.getenv("GOOGLE_API_KEY")
        self.genai_client = None

        # 로컬 환경 배려: Qdrant URL이 없으면 비활성화
        self._is_configured = bool(self.qdrant_url)

        if not self._is_configured:
            logger.warning("⚠️ [Qdrant] QDRANT_URL이 설정되지 않았습니다. Vector DB 기능이 비활성화됩니다.")
            logger.warning("   필요한 환경변수: QDRANT_URL, QDRANT_API_KEY (선택)")
            self.client = None
        else:
            try:
                # ✅ [작업 2] prefer_grpc=False 설정 추가 (REST 통신 안정성)
                self.client = AsyncQdrantClient(
                    url=self.qdrant_url,
                    api_key=self.qdrant_api_key,
                    timeout=30,
                    prefer_grpc=False  # REST API 사용 강제
                )
                logger.info(f"✅ [Qdrant] Vector DB 클라이언트 초기화 완료: {self.qdrant_url} (prefer_grpc=False)")
            except Exception as e:
                logger.error(f"❌ [Qdrant] 초기화 실패: {e}")
                self.client = None
                self._is_configured = False

        # ✅ [작업 1] Google GenAI 클라이언트 초기화
        if self.google_api_key:
            try:
                self.genai_client = genai.Client(api_key=self.google_api_key)
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
        ✅ [작업 1] Google GenAI 최신 SDK를 사용하여 텍스트를 벡터로 변환 (비동기)

        Args:
            text: 임베딩할 텍스트

        Returns:
            임베딩 벡터 (768차원) 또는 None
        """
        if not self.genai_client:
            logger.warning("⚠️ [Qdrant] Google GenAI 클라이언트가 초기화되지 않았습니다. 임베딩 생성을 건너뜁니다.")
            return None

        # ✅ [작업 4] 예외 처리로 시스템 중단 방지
        try:
            # 동기 함수를 비동기로 래핑 (FastAPI 이벤트 루프 블로킹 방지)
            def _sync_embed():
                response = self.genai_client.models.embed_content(
                    model='text-embedding-004',
                    contents=text
                )
                return response.embeddings[0].values

            # asyncio.to_thread로 블로킹 없이 실행
            embedding = await asyncio.to_thread(_sync_embed)
            return embedding

        except Exception as e:
            logger.error(f"❌ [Qdrant] Google GenAI 임베딩 생성 실패: {e}")
            return None

    async def get_embedding(self, text: str) -> Optional[List[float]]:
        """텍스트를 벡터로 변환 (Gemini 사용)"""
        return await self.get_gemini_embedding(text)

    async def upsert_memory(
        self,
        npc_id: int,
        scenario_id: int,
        text: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        NPC 기억/대화 기록을 Vector DB에 저장

        Args:
            npc_id: NPC ID
            scenario_id: 시나리오 ID
            text: 저장할 텍스트 (대화 내용, 설정 등)
            metadata: 추가 메타데이터 (timestamp, event_type 등)

        Returns:
            성공 여부
        """
        if not self.is_available:
            logger.warning("⚠️ [Qdrant] Vector DB를 사용할 수 없어 기억 저장을 건너뜁니다.")
            return False

        # ✅ [작업 4] 임베딩 생성 실패 시 시스템이 뻗지 않도록 예외 처리
        try:
            # 텍스트를 벡터로 변환 (Gemini 사용)
            vector = await self.get_gemini_embedding(text)
            if not vector:
                logger.warning("⚠️ [Qdrant] 임베딩 생성 실패 - 기억 저장을 건너뜁니다.")
                return False

            # 메타데이터 준비
            payload = {
                "npc_id": npc_id,
                "scenario_id": scenario_id,
                "text": text,
                **(metadata or {})
            }

            # Qdrant에 삽입
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

            logger.info(f"💾 [Qdrant] 기억 저장 완료: NPC={npc_id}, Scenario={scenario_id}")
            return True

        except Exception as e:
            logger.error(f"❌ [Qdrant] 기억 저장 실패: {e}")
            return False

    async def search_memory(
        self,
        query: str,
        npc_id: Optional[int] = None,
        scenario_id: Optional[int] = None,
        limit: int = 5
    ) -> List[Dict[str, Any]]:
        """
        유사한 기억/대화 검색

        Args:
            query: 검색 쿼리 (자연어)
            npc_id: 특정 NPC의 기억만 검색 (선택)
            scenario_id: 특정 시나리오의 기억만 검색 (선택)
            limit: 반환할 최대 결과 수

        Returns:
            검색 결과 리스트 (score, text, metadata 포함)
        """
        if not self.is_available:
            logger.warning("⚠️ [Qdrant] Vector DB를 사용할 수 없어 기억 검색을 건너뜁니다.")
            return []

        # ✅ [작업 4] 임베딩 생성 실패 시 빈 리스트 반환
        try:
            # 쿼리를 벡터로 변환 (Gemini 사용)
            query_vector = await self.get_gemini_embedding(query)

            if not query_vector:
                logger.warning("⚠️ [Qdrant] 쿼리 임베딩 생성 실패 - 빈 결과 반환")
                return []

            # 필터 조건 구성
            query_filter = None
            if npc_id or scenario_id:
                must_conditions = []
                if npc_id:
                    must_conditions.append({
                        "key": "npc_id",
                        "match": {"value": npc_id}
                    })
                if scenario_id:
                    must_conditions.append({
                        "key": "scenario_id",
                        "match": {"value": scenario_id}
                    })

                query_filter = {"must": must_conditions}

            # 검색 실행
            results = await self.client.search(
                collection_name=self.collection_name,
                query_vector=query_vector,
                query_filter=query_filter,
                limit=limit
            )

            # 결과 포맷팅
            formatted_results = []
            for result in results:
                formatted_results.append({
                    "score": result.score,
                    "text": result.payload.get("text", ""),
                    "npc_id": result.payload.get("npc_id"),
                    "scenario_id": result.payload.get("scenario_id"),
                    "metadata": {k: v for k, v in result.payload.items()
                               if k not in ["text", "npc_id", "scenario_id"]}
                })

            logger.info(f"🔍 [Qdrant] 검색 완료: {len(formatted_results)}개 결과")
            return formatted_results

        except Exception as e:
            logger.error(f"❌ [Qdrant] 검색 실패: {e}")
            return []

    async def delete_npc_memories(self, npc_id: int) -> bool:
        """
        특정 NPC의 모든 기억 삭제

        Args:
            npc_id: NPC ID

        Returns:
            성공 여부
        """
        if not self.is_available:
            return False

        try:
            await self.client.delete(
                collection_name=self.collection_name,
                points_selector={
                    "filter": {
                        "must": [
                            {
                                "key": "npc_id",
                                "match": {"value": npc_id}
                            }
                        ]
                    }
                }
            )

            logger.info(f"🗑️ [Qdrant] NPC {npc_id}의 기억 삭제 완료")
            return True

        except Exception as e:
            logger.error(f"❌ [Qdrant] NPC 기억 삭제 실패: {e}")
            return False


# 싱글톤 인스턴스
_vector_db_client: Optional[VectorDBClient] = None


def get_vector_db_client() -> VectorDBClient:
    """Vector DB 클라이언트 싱글톤 인스턴스 반환"""
    global _vector_db_client
    if _vector_db_client is None:
        _vector_db_client = VectorDBClient()
    return _vector_db_client
