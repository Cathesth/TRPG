from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON
from datetime import datetime
import os

db = SQLAlchemy()

# 환경에 따라 JSON 타입 결정 (SQLite는 JSONB 미지원, PostgreSQL은 JSONB 사용)
db_uri = os.getenv('DATABASE_URL', '')
if 'postgresql' in db_uri or 'postgres' in db_uri:
    JSON_TYPE = JSONB
else:
    JSON_TYPE = JSON


class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.String(50), primary_key=True)  # username을 id로 사용
    password_hash = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(120), nullable=True)  # 이메일 필드 추가
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # 관계 설정 (유저가 삭제되면 시나리오도 삭제될지, 유지될지는 정책에 따라 설정 가능)
    scenarios = db.relationship('Scenario', backref='owner', lazy=True)


class Scenario(db.Model):
    __tablename__ = 'scenarios'

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    author_id = db.Column(db.String(50), db.ForeignKey('users.id'), nullable=True)  # null이면 익명/시스템

    # 시나리오 전체 데이터 (scenes, endings, variables 등 구조화된 JSON)
    data = db.Column(JSON_TYPE, nullable=False)

    is_public = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'title': self.title,
            'author': self.author_id or 'Anonymous',
            'data': self.data,
            'is_public': self.is_public,
            'created_at': self.created_at.timestamp(),
            'updated_at': self.updated_at.timestamp()
        }


class Preset(db.Model):
    __tablename__ = 'presets'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, default='')
    author_id = db.Column(db.String(50), db.ForeignKey('users.id'), nullable=True)

    # 프리셋 전체 데이터 (nodes, connections, globalNpcs, settings 등)
    data = db.Column(JSON_TYPE, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'author': self.author_id or 'Anonymous',
            'data': self.data,
            'created_at': self.created_at.timestamp(),
            'updated_at': self.updated_at.timestamp()
        }


class TempScenario(db.Model):
    """
    Draft 시스템: 편집 중인 시나리오의 임시 저장용 테이블
    최종 반영 전까지 이 테이블에서만 데이터를 수정
    """
    __tablename__ = 'temp_scenarios'

    id = db.Column(db.Integer, primary_key=True)
    original_scenario_id = db.Column(db.Integer, db.ForeignKey('scenarios.id'), nullable=False)
    editor_id = db.Column(db.String(50), db.ForeignKey('users.id'), nullable=False)

    # 편집 중인 시나리오 데이터
    data = db.Column(JSON_TYPE, nullable=False)

    # 메타 정보
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 관계 설정
    original_scenario = db.relationship('Scenario', backref='drafts')

    def to_dict(self):
        return {
            'id': self.id,
            'original_scenario_id': self.original_scenario_id,
            'editor_id': self.editor_id,
            'data': self.data,
            'created_at': self.created_at.timestamp() if self.created_at else None,
            'updated_at': self.updated_at.timestamp() if self.updated_at else None
        }


# [신규 추가] NPC/Enemy 저장을 위한 모델
class CustomNPC(db.Model):
    __tablename__ = 'custom_npcs'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    # npc 또는 enemy 구분
    type = db.Column(db.String(50), default='npc')
    # 상세 데이터 (성격, 배경, 스탯 등 JSON 통째로 저장)
    data = db.Column(JSON_TYPE, nullable=False)

    # 소유자 (로그인 유저가 만든 경우)
    author_id = db.Column(db.String(50), db.ForeignKey('users.id'), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'type': self.type,
            'data': self.data,
            'author': self.author_id,
            'created_at': self.created_at.timestamp()
        }


class ScenarioHistory(db.Model):
    """
    시나리오 변경 이력 테이블
    - Undo/Redo 기능을 위한 스냅샷 저장
    - Railway PostgreSQL 환경에서 영속적으로 관리
    """
    __tablename__ = 'scenario_history'

    id = db.Column(db.Integer, primary_key=True)
    scenario_id = db.Column(db.Integer, db.ForeignKey('scenarios.id'), nullable=False)
    editor_id = db.Column(db.String(50), db.ForeignKey('users.id'), nullable=False)

    # 변경 이력 정보
    action_type = db.Column(db.String(50), nullable=False)  # 'scene_edit', 'scene_add', 'scene_delete', 'ending_edit', 'reorder', 'prologue_edit' 등
    action_description = db.Column(db.String(255), nullable=False)  # 사용자에게 보여줄 설명

    # 스냅샷 데이터 (해당 시점의 전체 시나리오 데이터)
    snapshot_data = db.Column(JSON_TYPE, nullable=False)

    # 이력 순서 (같은 시나리오 내에서의 순서)
    sequence = db.Column(db.Integer, nullable=False)

    # 현재 위치 표시 (Undo/Redo 시 현재 위치)
    is_current = db.Column(db.Boolean, default=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # 관계 설정
    scenario = db.relationship('Scenario', backref='history_entries')

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
