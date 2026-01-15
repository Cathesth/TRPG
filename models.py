from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, ForeignKey, create_engine
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON
from datetime import datetime
import os
import uuid
import logging

logger = logging.getLogger(__name__)

# SQLAlchemy Base
Base = declarative_base()

# 환경에 따라 JSON 타입 결정 (SQLite는 JSONB 미지원, PostgreSQL은 JSONB 사용)
db_uri = os.getenv('DATABASE_URL', '')
if 'postgresql' in db_uri or 'postgres' in db_uri:
    JSON_TYPE = JSONB
else:
    JSON_TYPE = JSON

# Database URL 처리 (postgres:// -> postgresql://)
# 로컬 개발 시 trpg.db 사용
DATABASE_URL = os.getenv('DATABASE_URL',
                         f'sqlite:///{os.path.join(os.path.dirname(os.path.abspath(__file__)), "trpg.db")}')

if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Engine 및 Session 생성
# Railway PostgreSQL 최적화 설정 (로컬 개발 시에도 안전하게 동작)
try:
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        pool_recycle=3600,  # 1시간마다 연결 재활용 (Railway 타임아웃 방지)
        echo=False  # 프로덕션에서는 False
    )
    logger.info(f"✅ Database engine created: {DATABASE_URL.split('@')[-1] if '@' in DATABASE_URL else 'SQLite'}")
except Exception as e:
    logger.error(f"❌ Failed to create database engine: {e}")
    raise

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# Dependency - DB Session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class User(Base):
    __tablename__ = 'users'

    id = Column(String(50), primary_key=True)  # username을 id로 사용
    password_hash = Column(String(255), nullable=False)
    email = Column(String(120), nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    # 관계 설정
    scenarios = relationship('Scenario', back_populates='owner')
    # Flask-Login 관련 속성 삭제됨 (auth.py가 대신 처리함)


class Scenario(Base):
    __tablename__ = 'scenarios'

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String(100), unique=True, index=True)  # UUID
    title = Column(String(100), nullable=False, index=True)
    author_id = Column(String(50), ForeignKey('users.id'), nullable=True)

    # 시나리오 전체 데이터 (scenes, endings, variables 등 구조화된 JSON)
    data = Column(JSON_TYPE, nullable=False)

    is_public = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # 관계 설정
    owner = relationship('User', back_populates='scenarios')
    drafts = relationship('TempScenario', back_populates='original_scenario', cascade="all, delete-orphan")
    history_entries = relationship('ScenarioHistory', back_populates='scenario', cascade="all, delete-orphan")

    def to_dict(self):
        return {
            'id': self.id,
            'filename': self.filename,
            'title': self.title,
            'author': self.author_id or 'Anonymous',
            'data': self.data,
            'is_public': self.is_public,
            'created_at': self.created_at.timestamp() if self.created_at else None,
            'updated_at': self.updated_at.timestamp() if self.updated_at else None
        }


class Preset(Base):
    __tablename__ = 'presets'

    id = Column(Integer, primary_key=True, index=True)

    # 프론트엔드 호환용 식별자 (UUID)
    filename = Column(String(100), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))

    name = Column(String(100), nullable=False, index=True)
    description = Column(Text, default='')
    author_id = Column(String(50), ForeignKey('users.id'), nullable=True)

    # 프리셋 전체 데이터 (nodes, connections, globalNpcs, settings 등)
    data = Column(JSON_TYPE, nullable=False)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    def to_dict(self):
        return {
            'filename': self.filename,  # id 대신 filename 반환
            'name': self.name,
            'desc': self.description,
            'author': self.author_id or 'Anonymous',
            'data': self.data,
            'created_time': self.created_at.timestamp() if self.created_at else None
        }


class TempScenario(Base):
    """
    Draft 시스템: 편집 중인 시나리오의 임시 저장용 테이블
    최종 반영 전까지 이 테이블에서만 데이터를 수정
    """
    __tablename__ = 'temp_scenarios'

    id = Column(Integer, primary_key=True, index=True)
    original_scenario_id = Column(Integer, ForeignKey('scenarios.id', ondelete='CASCADE'), nullable=False)
    editor_id = Column(String(50), ForeignKey('users.id'), nullable=False)

    # 편집 중인 시나리오 데이터
    data = Column(JSON_TYPE, nullable=False)

    # 메타 정보
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # 관계 설정
    original_scenario = relationship('Scenario', back_populates='drafts')

    def to_dict(self):
        return {
            'id': self.id,
            'original_scenario_id': self.original_scenario_id,
            'editor_id': self.editor_id,
            'data': self.data,
            'created_at': self.created_at.timestamp() if self.created_at else None,
            'updated_at': self.updated_at.timestamp() if self.updated_at else None
        }


class CustomNPC(Base):
    """NPC/Enemy 저장을 위한 모델"""
    __tablename__ = 'custom_npcs'

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, index=True)
    type = Column(String(50), default='npc')  # npc 또는 enemy 구분
    data = Column(JSON_TYPE, nullable=False)  # 상세 데이터 JSON

    author_id = Column(String(50), ForeignKey('users.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'type': self.type,
            'data': self.data,
            'author': self.author_id,
            'created_at': self.created_at.timestamp() if self.created_at else None
        }


class ScenarioHistory(Base):
    """
    시나리오 변경 이력 테이블
    - Undo/Redo 기능을 위한 스냅샷 저장
    - Railway PostgreSQL 환경에서 영속적으로 관리
    """
    __tablename__ = 'scenario_histories'  # 복수형 권장

    id = Column(Integer, primary_key=True, index=True)
    scenario_id = Column(Integer, ForeignKey('scenarios.id', ondelete='CASCADE'), nullable=False)
    editor_id = Column(String(50), ForeignKey('users.id'), nullable=False)

    # 변경 이력 정보
    action_type = Column(String(50), nullable=False)
    action_description = Column(String(255), nullable=False)

    # 스냅샷 데이터
    snapshot_data = Column(JSON_TYPE, nullable=False)

    # 이력 순서
    sequence = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.now)

    # 관계 설정
    scenario = relationship('Scenario', back_populates='history_entries')


class GameSession(Base):
    """
    🛠️ 게임 세션 저장 테이블 (WorldState 영속성 관리)

    세션은 휘발성이므로, WorldState를 DB에 저장하여
    유저가 게임을 종료하고 다시 시작해도 진행 상황을 복원

    Railway PostgreSQL 환경 최적화:
    - JSONB 타입 사용 (쿼리 성능 향상)
    - 인덱스 설정 (session_key, user_id, scenario_id)
    - 자동 정리 (오래된 세션 삭제)
    """
    __tablename__ = 'game_sessions'

    id = Column(Integer, primary_key=True)

    # 세션 식별자 (인덱스 추가)
    user_id = Column(String(50), ForeignKey('users.id'), nullable=True, index=True)
    session_key = Column(String(100), unique=True, nullable=False, default=lambda: str(uuid.uuid4()), index=True)

    # 시나리오 정보 (인덱스 추가)
    scenario_id = Column(Integer, ForeignKey('scenarios.id', ondelete='CASCADE'), nullable=False, index=True)

    # 게임 상태 (PlayerState 전체 직렬화) - JSONB로 효율적 저장
    player_state = Column(JSON_TYPE, nullable=False)

    # WorldState 스냅샷 (규칙 기반 상태) - JSONB로 효율적 저장
    world_state = Column(JSON_TYPE, nullable=False)

    # 메타 정보
    current_scene_id = Column(String(100), nullable=False, index=True)
    turn_count = Column(Integer, default=0)

    # 타임스탬프 (인덱스 추가 - 오래된 세션 정리용)
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    last_played_at = Column(DateTime, default=datetime.now, index=True)

    def to_dict(self):
        return {
            'id': self.id,
            'session_key': self.session_key,
            'user_id': self.user_id,
            'scenario_id': self.scenario_id,
            'current_scene_id': self.current_scene_id,
            'turn_count': self.turn_count,
            'player_state': self.player_state,
            'world_state': self.world_state,
            'created_at': self.created_at.timestamp() if self.created_at else None,
            'updated_at': self.updated_at.timestamp() if self.updated_at else None,
            'last_played_at': self.last_played_at.timestamp() if self.last_played_at else None
        }


# models.py 파일 내 적절한 위치에 추가 (Base 클래스 정의 이후)
class ScenarioLike(Base):
    __tablename__ = "scenario_likes"
    user_id = Column(String, ForeignKey("users.id"), primary_key=True)
    scenario_id = Column(Integer, ForeignKey("scenarios.id"), primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# 테이블 생성 함수
def create_tables():
    """Railway PostgreSQL에 테이블 생성"""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("✅ All database tables created successfully")
    except Exception as e:
        logger.error(f"❌ Failed to create tables: {e}")
        raise


# 오래된 세션 정리 함수 (Railway 리소스 최적화)
def cleanup_old_sessions(days=7):
    """
    N일 이상 접근하지 않은 세션 삭제

    Args:
        days: 보관 기간 (기본 7일)
    """
    try:
        from datetime import timedelta
        db = SessionLocal()
        cutoff_date = datetime.now() - timedelta(days=days)

        deleted_count = db.query(GameSession).filter(
            GameSession.last_played_at < cutoff_date
        ).delete()

        db.commit()
        db.close()

        logger.info(f"🧹 Cleaned up {deleted_count} old game sessions")
        return deleted_count
    except Exception as e:
        logger.error(f"❌ Failed to cleanup sessions: {e}")
        return 0



