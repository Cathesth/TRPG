"""
Routes 모듈 - Flask 라우트 정의
"""
from .views import views_bp
from .api import api_bp
from .game import game_bp

__all__ = ['views_bp', 'api_bp', 'game_bp']

