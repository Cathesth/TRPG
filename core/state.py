"""
게임 상태 관리 싱글톤
"""
from typing import Dict, Any, Optional
from config import DEFAULT_CONFIG


class GameState:
    """
    게임 상태를 관리하는 싱글톤 클래스
    여러 모듈에서 공유되는 상태를 중앙에서 관리
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        """초기 상태 설정"""
        self._config = DEFAULT_CONFIG.copy()
        self._state: Optional[Dict[str, Any]] = None
        self._game_graph = None

    @property
    def config(self) -> Dict[str, Any]:
        return self._config

    @config.setter
    def config(self, value: Dict[str, Any]):
        self._config = value

    @property
    def state(self) -> Optional[Dict[str, Any]]:
        return self._state

    @state.setter
    def state(self, value: Optional[Dict[str, Any]]):
        self._state = value

    @property
    def game_graph(self):
        return self._game_graph

    @game_graph.setter
    def game_graph(self, value):
        self._game_graph = value

    def clear(self):
        """상태 초기화"""
        self._state = None
        self._game_graph = None

    def is_loaded(self) -> bool:
        """게임이 로드되었는지 확인"""
        return self._state is not None


# 전역 인스턴스
game_state = GameState()

