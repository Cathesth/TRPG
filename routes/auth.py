"""
FastAPI 인증 헬퍼 모듈
- 세션 기반 사용자 인증
- Flask-Login 대체
"""
from fastapi import Request, HTTPException, Depends
from typing import Optional

from models import SessionLocal, User


class CurrentUser:
    """현재 로그인한 사용자 정보"""
    def __init__(self, user: Optional[User] = None):
        self._user = user

    @property
    def is_authenticated(self) -> bool:
        return self._user is not None

    @property
    def id(self) -> Optional[str]:
        return self._user.id if self._user else None

    @property
    def profile_img(self) -> str:
        """기본 프로필 이미지 (Gravatar 스타일)"""
        if self._user:
            # 간단한 해시 기반 아바타 URL
            return f"https://api.dicebear.com/7.x/initials/svg?seed={self._user.id}&backgroundColor=6E47E6"
        return "https://api.dicebear.com/7.x/shapes/svg?seed=guest"

    def __bool__(self):
        return self.is_authenticated


def get_user_from_session(request: Request) -> Optional[User]:
    """세션에서 사용자 정보 가져오기"""
    user_id = request.session.get("user_id")
    if not user_id:
        return None

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        return user
    finally:
        db.close()


def get_current_user_optional(request: Request) -> CurrentUser:
    """현재 사용자 (로그인 선택적)"""
    user = get_user_from_session(request)
    return CurrentUser(user)


def get_current_user(request: Request) -> CurrentUser:
    """현재 사용자 (로그인 필수)"""
    user = get_user_from_session(request)
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    return CurrentUser(user)


def login_user(request: Request, user: User):
    """사용자 로그인 (세션에 저장)"""
    request.session["user_id"] = user.id


def logout_user(request: Request):
    """사용자 로그아웃 (세션에서 제거)"""
    request.session.pop("user_id", None)
