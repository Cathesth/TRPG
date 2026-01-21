#!/usr/bin/env python
"""
Railway PostgreSQL 데이터베이스 마이그레이션 스크립트

실행 방법:
    python migrate_db.py
"""
import logging
from sqlalchemy import text
from models import engine, SessionLocal

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)


def run_migration():
    """데이터베이스 마이그레이션 실행"""
    db = SessionLocal()

    try:
        logger.info("🚀 Starting database migration...")

        # 1. scenarios 테이블에 filename 컬럼 추가 (없으면)
        logger.info("📋 Adding filename column to scenarios table...")
        try:
            db.execute(text("""
                ALTER TABLE scenarios 
                ADD COLUMN IF NOT EXISTS filename VARCHAR(100) UNIQUE;
            """))
            db.commit()
            logger.info("✅ filename column added successfully")
        except Exception as e:
            logger.warning(f"⚠️ filename column might already exist: {e}")
            db.rollback()

        # 2. 기존 데이터에 filename 값 생성 (UUID)
        logger.info("📋 Generating filename values for existing scenarios...")
        try:
            db.execute(text("""
                UPDATE scenarios 
                SET filename = CONCAT('scenario_', id::text, '_', 
                    substr(md5(random()::text), 1, 8))
                WHERE filename IS NULL;
            """))
            db.commit()
            logger.info("✅ filename values generated successfully")
        except Exception as e:
            logger.warning(f"⚠️ Failed to generate filename values: {e}")
            db.rollback()

        # 3. scenarios 테이블에 인덱스 추가
        logger.info("📋 Adding indexes to scenarios table...")
        try:
            db.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_scenarios_id ON scenarios(id);
            """))
            db.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_scenarios_filename ON scenarios(filename);
            """))
            db.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_scenarios_title ON scenarios(title);
            """))
            db.commit()
            logger.info("✅ Indexes added successfully")
        except Exception as e:
            logger.warning(f"⚠️ Indexes might already exist: {e}")
            db.rollback()

        # 4. presets 테이블에 인덱스 추가
        logger.info("📋 Adding indexes to presets table...")
        try:
            db.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_presets_id ON presets(id);
            """))
            db.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_presets_name ON presets(name);
            """))
            db.commit()
            logger.info("✅ Presets indexes added successfully")
        except Exception as e:
            logger.warning(f"⚠️ Presets indexes might already exist: {e}")
            db.rollback()

        # 5. custom_npcs 테이블에 인덱스 추가
        logger.info("📋 Adding indexes to custom_npcs table...")
        try:
            db.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_custom_npcs_id ON custom_npcs(id);
            """))
            db.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_custom_npcs_name ON custom_npcs(name);
            """))
            db.commit()
            logger.info("✅ Custom NPCs indexes added successfully")
        except Exception as e:
            logger.warning(f"⚠️ Custom NPCs indexes might already exist: {e}")
            db.rollback()

        # 6. temp_scenarios 테이블에 인덱스 추가
        logger.info("📋 Adding indexes to temp_scenarios table...")
        try:
            db.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_temp_scenarios_id ON temp_scenarios(id);
            """))
            db.commit()
            logger.info("✅ Temp scenarios indexes added successfully")
        except Exception as e:
            logger.warning(f"⚠️ Temp scenarios indexes might already exist: {e}")
            db.rollback()

        # 7. scenario_histories 테이블 이름 확인 및 인덱스 추가
        logger.info("📋 Adding indexes to scenario_histories table...")
        try:
            # 먼저 scenario_history 테이블이 있는지 확인
            result = db.execute(text("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_name = 'scenario_history'
                );
            """))
            has_old_table = result.scalar()

            if has_old_table:
                # 기존 테이블 이름 변경
                logger.info("📋 Renaming scenario_history to scenario_histories...")
                db.execute(text("""
                    ALTER TABLE scenario_history 
                    RENAME TO scenario_histories;
                """))
                db.commit()
                logger.info("✅ Table renamed successfully")

            db.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_scenario_histories_id 
                ON scenario_histories(id);
            """))
            db.commit()
            logger.info("✅ Scenario histories indexes added successfully")
        except Exception as e:
            logger.warning(f"⚠️ Scenario histories migration issue: {e}")
            db.rollback()

        logger.info("✅ Database migration completed successfully!")
        return True

    except Exception as e:
        logger.error(f"❌ Database migration failed: {e}")
        import traceback
        traceback.print_exc()
        db.rollback()
        return False

    finally:
        db.close()


if __name__ == "__main__":
    success = run_migration()
    exit(0 if success else 1)

