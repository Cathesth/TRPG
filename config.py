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

# 레거시 호환용 (필요 시 사용)
DB_FOLDER = os.path.join(BASE_DIR, 'DB')

DEFAULT_PLAYER_VARS = {
    "hp": 100,
    "sanity": 100,
    "inventory": [],
    "gold": 0
}