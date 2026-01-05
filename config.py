import os

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

# [ADD] views.py 호환용 버전 함수
def get_full_version():
    return "v1.0.0 (Railway)"