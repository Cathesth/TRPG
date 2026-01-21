import os
import subprocess
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

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
PRESETS_FOLDER = os.path.join(DB_FOLDER, 'presets')

# state.py 오류 방지용 기본 설정
DEFAULT_CONFIG = {
    "title": "New TRPG Scenario",
    "genre": "Adventure",
    "model": "openai/tngtech/llama-3-8b-tool-v1"
}

# [FIX] 누락되었던 플레이어 기본 변수 설정 추가
DEFAULT_PLAYER_VARS = {
    "hp": 100,
    "sanity": 100,
    "inventory": [],
    "gold": 0
}


# --- [NEW] 토큰 경제 시스템 설정 (1K 토큰 기준) ---
class TokenConfig:
    """
    토큰 소모 정책 설정

    [화폐 단위: Credit]
    1 Credit = $0.0001 (0.01센트)

    [설정 기준: 1,000 토큰(1K) 당 소모 Credit]
    - 1M 기준보다 직관적임 (턴당 수천 토큰 사용하므로)
    - 예: Gemini Input 1K = 1 Credit
    """

    # [1] 고정 비용
    COST_IMAGE_GENERATION = 50  # 이미지 1장 ($0.005)
    COST_SCENE_ADD = 10  # 씬 추가 ($0.001)

    # [2] 모델별 비용 (1,000 토큰 당 소모 Credit)
    MODEL_COSTS = {
        # 1. Google: Gemini 2.0 Flash ($0.10 / $0.40 per M) -> (1 / 4 Cr)
        "gemini-2.0-flash": {"input": 1.0, "output": 4.0},
        "gemini-2.5-flash": {"input": 1.0, "output": 4.0},

        "claude-3.5-sonnet": {"input": 60.0, "output": 150.0},

        # 3. OpenAI: GPT-4o ($2.50 / $10.00 per M) -> (25 / 100 Cr)
        "gpt-4o": {"input": 25.0, "output": 100.0},
        "gpt-4o-mini": {"input": 1.5, "output": 6.0},

        # 4. Free Models
        "deepseek": {"input": 0.0, "output": 0.0},
        "llama-3": {"input": 0.0, "output": 0.0},

        # Fallback (GPT-4o Mini 수준)
        "default": {"input": 1.5, "output": 6.0}
    }

    # [3] 신규 가입 시 지급 토큰 (1000 Credit = $0.10)
    INITIAL_TOKEN_BALANCE = 1000


# 버전 정보 설정
VERSION_NUMBER = 0


def get_git_commit_hash():
    """Git 커밋 해시 가져오기 (짧은 버전)"""
    railway_commit = os.getenv('RAILWAY_GIT_COMMIT_SHA')
    if railway_commit:
        return railway_commit[:8]

    try:
        hash_val = subprocess.check_output(
            'git rev-parse --short=8 HEAD',
            cwd=BASE_DIR,
            stderr=subprocess.DEVNULL,
            shell=True,
            timeout=5
        ).decode('utf-8').strip()

        if hash_val and len(hash_val) == 8 and all(c in '0123456789abcdef' for c in hash_val.lower()):
            return hash_val
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
        pass
    except Exception as e:
        logger.warning(f"Git 커밋 해시 가져오기 실패: {e}")

    return 'dev'


# [FIX] app.py에서 호출하는 함수
def get_full_version():
    """전체 버전 문자열 생성: 년.월일.넘버.해시"""
    now = datetime.now()
    year = now.year
    month_day = now.strftime('%m%d')
    commit_hash = get_git_commit_hash()

    # Railway 브랜치 정보도 표시 (선택사항)
    railway_branch = os.getenv('RAILWAY_GIT_BRANCH')
    if railway_branch and railway_branch != 'main':
        return f"{year}.{month_day}.{VERSION_NUMBER}.{commit_hash}-{railway_branch}"

    return f"{year}.{month_day}.{VERSION_NUMBER}.{commit_hash}"