"""
Routes 모듈 - FastAPI 라우터 정의
"""
from .views import views_router
from .api import api_router
from .game import game_router

__all__ = ['views_router', 'api_router', 'game_router']
