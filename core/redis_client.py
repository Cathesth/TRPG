"""
Redis 클라이언트 (Railway Redis 연동)
로컬 Redis 설치 없이도 구동 가능하도록 설계
FastAPI 비동기 환경에 최적화된 redis.asyncio 사용
"""
import os
import json
import logging
from typing import Optional, Any

try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    aioredis = None

logger = logging.getLogger(__name__)


class RedisClient:
    """
    비동기 Redis 클라이언트 (연결 풀 사용)
    REDIS_URL 환경변수가 없으면 연결 시도를 하지 않음
    """

    def __init__(self):
        self.redis_url = os.getenv("REDIS_URL")
        self.pool: Optional[Any] = None
        self.client: Optional[Any] = None
        self.is_connected = False

        if not self.redis_url:
            logger.info("⚠️ [REDIS] REDIS_URL not found - Redis disabled (running in local mode)")
            return

        if not REDIS_AVAILABLE:
            logger.warning("⚠️ [REDIS] redis.asyncio not installed - Redis disabled")
            return

        logger.info(f"✅ [REDIS] Redis URL configured: {self.redis_url[:20]}...")

    async def connect(self):
        """Redis 연결 풀 생성 (필요시)"""
        if not self.redis_url or not REDIS_AVAILABLE:
            return

        if self.is_connected and self.client:
            return

        try:
            # 연결 풀 생성 (성능 최적화)
            self.pool = aioredis.ConnectionPool.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True,
                max_connections=10,
                socket_connect_timeout=5,
                socket_timeout=5
            )

            # 연결 풀을 사용하는 클라이언트 생성
            self.client = aioredis.Redis(connection_pool=self.pool)

            # 연결 테스트
            await self.client.ping()
            self.is_connected = True
            logger.info("✅ [REDIS] Connected successfully with connection pool")
        except Exception as e:
            logger.error(f"❌ [REDIS] Connection failed: {e}")
            self.client = None
            self.pool = None
            self.is_connected = False

    async def disconnect(self):
        """Redis 연결 종료"""
        if self.client:
            try:
                await self.client.close()
                logger.info("🔌 [REDIS] Client disconnected")
            except Exception as e:
                logger.error(f"❌ [REDIS] Client disconnect error: {e}")
            finally:
                self.client = None

        if self.pool:
            try:
                await self.pool.disconnect()
                logger.info("🔌 [REDIS] Connection pool closed")
            except Exception as e:
                logger.error(f"❌ [REDIS] Pool disconnect error: {e}")
            finally:
                self.pool = None

        self.is_connected = False

    async def get(self, key: str) -> Optional[dict]:
        """
        Redis에서 데이터 가져오기 (JSON 역직렬화)

        Args:
            key: Redis 키

        Returns:
            딕셔너리 또는 None
        """
        if not self.is_connected or not self.client:
            return None

        try:
            data = await self.client.get(key)
            if data:
                return json.loads(data)
            return None
        except json.JSONDecodeError as e:
            logger.error(f"❌ [REDIS] JSON decode error for key '{key}': {e}")
            return None
        except Exception as e:
            logger.error(f"❌ [REDIS] Get error for key '{key}': {e}")
            return None

    async def set(self, key: str, value: dict, expire: Optional[int] = None) -> bool:
        """
        Redis에 데이터 저장 (JSON 직렬화)

        Args:
            key: Redis 키
            value: 저장할 딕셔너리
            expire: TTL (초) - None이면 만료 없음

        Returns:
            성공 여부
        """
        if not self.is_connected or not self.client:
            return False

        try:
            serialized = json.dumps(value, ensure_ascii=False)
            if expire:
                await self.client.setex(key, expire, serialized)
            else:
                await self.client.set(key, serialized)
            logger.debug(f"✅ [REDIS] Data saved to key: {key}")
            return True
        except Exception as e:
            logger.error(f"❌ [REDIS] Set error for key '{key}': {e}")
            return False

    async def delete(self, key: str) -> bool:
        """
        Redis에서 데이터 삭제

        Args:
            key: Redis 키

        Returns:
            성공 여부
        """
        if not self.is_connected or not self.client:
            return False

        try:
            await self.client.delete(key)
            logger.debug(f"🗑️ [REDIS] Key deleted: {key}")
            return True
        except Exception as e:
            logger.error(f"❌ [REDIS] Delete error for key '{key}': {e}")
            return False

    async def exists(self, key: str) -> bool:
        """
        Redis에 키가 존재하는지 확인

        Args:
            key: Redis 키

        Returns:
            존재 여부
        """
        if not self.is_connected or not self.client:
            return False

        try:
            result = await self.client.exists(key)
            return bool(result)
        except Exception as e:
            logger.error(f"❌ [REDIS] Exists check error for key '{key}': {e}")
            return False


# 전역 인스턴스 (FastAPI 앱에서 사용)
redis_client = RedisClient()
