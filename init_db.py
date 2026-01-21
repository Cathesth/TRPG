#!/usr/bin/env python
"""
Railway PostgreSQL 데이터베이스 초기화 스크립트

실행 방법:
    python init_db.py
"""
import logging
from models import create_tables, cleanup_old_sessions

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)


def main():
    """데이터베이스 초기화 메인 함수"""
    try:
        logger.info("🚀 Starting database initialization...")

        # 1. 테이블 생성
        logger.info("📋 Creating tables...")
        create_tables()

        # 2. 마이그레이션 실행 (컬럼 추가)
        logger.info("📋 Running migrations...")
        try:
            from migrate_db import run_migration
            run_migration()
        except Exception as e:
            logger.warning(f"⚠️ Migration skipped or failed: {e}")

        # 3. 오래된 세션 정리 (선택사항)
        logger.info("🧹 Cleaning up old sessions...")
        deleted = cleanup_old_sessions(days=7)

        logger.info(f"✅ Database initialization completed successfully!")
        logger.info(f"   - Tables created")
        logger.info(f"   - Migrations applied")
        logger.info(f"   - {deleted} old sessions cleaned up")

        return True

    except Exception as e:
        logger.error(f"❌ Database initialization failed: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
