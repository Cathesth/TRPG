from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, ForeignKey, create_engine
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON
from datetime import datetime
import os

# SQLAlchemy Base
Base = declarative_base()

# 환경에 따라 JSON 타입 결정 (SQLite는 JSONB 미지원, PostgreSQL은 JSONB 사용)
db_uri = os.getenv('DATABASE_URL', '')
if 'postgresql' in db_uri or 'postgres' in db_uri:
    JSON_TYPE = JSONB
else:
    JSON_TYPE = JSON

# Database URL 처리 (postgres:// -> postgresql://)
DATABASE_URL = os.getenv('DATABASE_URL', f'sqlite:///{os.path.join(os.path.dirname(os.path.abspath(__file__)), "trpg.db")}')
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Engine 및 Session 생성
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
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
    created_at = Column(DateTime, default=datetime.utcnow)

    # 관계 설정
    scenarios = relationship('Scenario', back_populates='owner')

    # Flask-Login 호환 속성
    @property
    def is_authenticated(self):
        return True

    @property
    def is_active(self):
        return True

    @property
    def is_anonymous(self):
        return False

    def get_id(self):
        return self.id


class Scenario(Base):
    __tablename__ = 'scenarios'

    id = Column(Integer, primary_key=True)
    title = Column(String(100), nullable=False)
    author_id = Column(String(50), ForeignKey('users.id'), nullable=True)

    # 시나리오 전체 데이터 (scenes, endings, variables 등 구조화된 JSON)
    data = Column(JSON_TYPE, nullable=False)

    is_public = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 관계 설정
    owner = relationship('User', back_populates='scenarios')
    drafts = relationship('TempScenario', back_populates='original_scenario')
    history_entries = relationship('ScenarioHistory', back_populates='scenario')

    def to_dict(self):
        return {
            'id': self.id,
            'title': self.title,
            'author': self.author_id or 'Anonymous',
            'data': self.data,
            'is_public': self.is_public,
            'created_at': self.created_at.timestamp() if self.created_at else None,
            'updated_at': self.updated_at.timestamp() if self.updated_at else None
        }


class Preset(Base):
    __tablename__ = 'presets'

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, default='')
    author_id = Column(String(50), ForeignKey('users.id'), nullable=True)

    # 프리셋 전체 데이터 (nodes, connections, globalNpcs, settings 등)
    data = Column(JSON_TYPE, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'author': self.author_id or 'Anonymous',
            'data': self.data,
            'created_at': self.created_at.timestamp() if self.created_at else None,
            'updated_at': self.updated_at.timestamp() if self.updated_at else None
        }


class TempScenario(Base):
    """
    Draft 시스템: 편집 중인 시나리오의 임시 저장용 테이블
    최종 반영 전까지 이 테이블에서만 데이터를 수정
    """
    __tablename__ = 'temp_scenarios'

    id = Column(Integer, primary_key=True)
    original_scenario_id = Column(Integer, ForeignKey('scenarios.id'), nullable=False)
    editor_id = Column(String(50), ForeignKey('users.id'), nullable=False)

    # 편집 중인 시나리오 데이터
    data = Column(JSON_TYPE, nullable=False)

    # 메타 정보
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    type = Column(String(50), default='npc')  # npc 또는 enemy 구분
    data = Column(JSON_TYPE, nullable=False)  # 상세 데이터 JSON

    author_id = Column(String(50), ForeignKey('users.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

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
    __tablename__ = 'scenario_history'

    id = Column(Integer, primary_key=True)
    scenario_id = Column(Integer, ForeignKey('scenarios.id', ondelete='CASCADE'), nullable=False)
    editor_id = Column(String(50), ForeignKey('users.id'), nullable=False)

    # 변경 이력 정보
    action_type = Column(String(50), nullable=False)
    action_description = Column(String(255), nullable=False)

    # 스냅샷 데이터
    snapshot_data = Column(JSON_TYPE, nullable=False)

    # 이력 순서
    sequence = Column(Integer, nullable=False)

    # 현재 위치 표시
    is_current = Column(Boolean, default=False)

    created_at = Column(DateTime, default=datetime.utcnow)

    # 관계 설정
    scenario = relationship('Scenario', back_populates='history_entries')

    def to_dict(self):
        return {
            'id': self.id,
            'scenario_id': self.scenario_id,
            'editor_id': self.editor_id,
            'action_type': self.action_type,
            'action_description': self.action_description,
            'sequence': self.sequence,
            'is_current': self.is_current,
            'created_at': self.created_at.timestamp() if self.created_at else None
        }


# 테이블 생성 함수
def create_tables():
    Base.metadata.create_all(bind=engine)
