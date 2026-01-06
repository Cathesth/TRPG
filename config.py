import os
import subprocess
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Railway 등에서 제공하는 DATABASE_URL 사용
# 로컬 개발 시에는 fallback으로 sqlite 사용 (trpg.db)
SQLALCHEMY_DATABASE_URI = os.getenv('DATABASE_URL', f'sqlite:///{os.path.join(BASE_DIR, "trpg.db")}')

# SQLAlchemy 1.4+ 호환성 처리 (postgres:// -> postgresql://)
if SQLALCHEMY_DATABASE_URI and SQLALCHEMY_DATABASE_URI.startswith("postgres://"):
    SQLALCHEMY_DATABASE_URI = SQLALCHEMY_DATABASE_URI.replace("postgres://", "postgresql://", 1)

SQLALCHEMY_TRACK_MODIFICATIONS = False

LOG_FORMAT = '[%(asctime)s] %(levelname)s in %(module)s: %(message)s'
LOG_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'

# 레거시 파일 시스템 호환용
DB_FOLDER = os.path.join(BASE_DIR, 'DB')
# [CRITICAL FIX] PresetService에서 참조하는 경로 추가
PRESETS_FOLDER = os.path.join(DB_FOLDER, 'presets')

# [CRITICAL FIX] state.py 오류 방지용 기본 설정
DEFAULT_CONFIG = {
    "title": "New TRPG Scenario",
    "genre": "Adventure",
    "model": "openai/tngtech/deepseek-r1t2-chimera:free"
}

DEFAULT_PLAYER_VARS = {
    "hp": 100,
    "sanity": 100,
    "inventory": [],
    "gold": 0
}

# 버전 정보 설정
VERSION_NUMBER = 0  # 수동으로 증가시킬 넘버

def get_git_commit_hash():
    """Git 커밋 해시 가져오기 (짧은 버전)"""
    try:
        hash_val = subprocess.check_output(
            ['git', 'rev-parse', '--short=8', 'HEAD'],
            cwd=BASE_DIR,
            stderr=subprocess.DEVNULL
        ).decode('ascii').strip()
        return hash_val
    except:
        return 'unknown'

def get_full_version():
    """전체 버전 문자열 생성: 년.월일.넘버.해시"""
    now = datetime.now()
    year = now.year
    month_day = now.strftime('%m%d')
    commit_hash = get_git_commit_hash()
    return f"{year}.{month_day}.{VERSION_NUMBER}.{commit_hash}"
