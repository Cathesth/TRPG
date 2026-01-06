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
    # 1. Railway 환경변수 우선 확인 (배포 시)
    railway_commit = os.getenv('RAILWAY_GIT_COMMIT_SHA')
    if railway_commit:
        # Railway는 전체 해시를 제공하므로 앞 8자리만 사용
        return railway_commit[:8]

    # 2. 로컬 Git 명령어로 시도
    try:
        # Windows 환경에서 shell=True 추가
        hash_val = subprocess.check_output(
            'git rev-parse --short=8 HEAD',
            cwd=BASE_DIR,
            stderr=subprocess.DEVNULL,
            shell=True,
            timeout=5
        ).decode('utf-8').strip()

        # 유효한 해시인지 검증 (16진수 8자리)
        if hash_val and len(hash_val) == 8 and all(c in '0123456789abcdef' for c in hash_val.lower()):
            return hash_val
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
        pass
    except Exception as e:
        logger.warning(f"Git 커밋 해시 가져오기 실패: {e}")

    # 3. Fallback: 개발 환경
    return 'dev'

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
