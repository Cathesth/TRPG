"""
애플리케이션 설정 및 상수 정의
"""
import os
import subprocess

# 기본 경로 설정
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FOLDER = os.path.join(BASE_DIR, 'DB', 'scenarios')
PRESETS_FOLDER = os.path.join(BASE_DIR, 'DB', 'presets')

# 버전 정보
APP_VERSION = "2026.0105.0"

def get_git_commit_hash():
    """현재 Git 커밋 해시 (short) 가져오기"""
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--short', 'HEAD'],
            capture_output=True,
            text=True,
            timeout=2,
            cwd=BASE_DIR
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"

def get_full_version():
    """전체 버전 문자열 생성 (예: 2026.0105.0.abc1234)"""
    commit_hash = get_git_commit_hash()
    return f"{APP_VERSION}.{commit_hash}"

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
