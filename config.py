"""
애플리케이션 설정 및 상수 정의
"""
import os

# 기본 경로 설정
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FOLDER = os.path.join(BASE_DIR, 'DB', 'scenarios')
PRESETS_FOLDER = os.path.join(BASE_DIR, 'DB', 'presets')

# 기본 게임 설정
DEFAULT_CONFIG = {
    "title": "미정",
    "dice_system": "1d20"
}

# 기본 플레이어 변수
DEFAULT_PLAYER_VARS = {
    "hp": 100,
    "inventory": []
}

# 로깅 설정
LOG_FORMAT = '[%(asctime)s] %(levelname)s in %(module)s: %(message)s'
LOG_DATE_FORMAT = '%H:%M:%S'

