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
