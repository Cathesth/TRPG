import logging
from werkzeug.security import generate_password_hash, check_password_hash
from models import db, User
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

class UserService:
    @staticmethod
    def create_user(username, password, email=None) -> bool:
        try:
            password_hash = generate_password_hash(password)
            new_user = User(id=username, password_hash=password_hash, email=email)
            db.session.add(new_user)
            db.session.commit()
            return True
        except IntegrityError:
            db.session.rollback()
            return False
        except Exception as e:
            logger.error(f"Create User Error: {e}")
            db.session.rollback()
            return False

    @staticmethod
    def verify_user(username, password):
        try:
            user = User.query.get(username)
            if user and check_password_hash(user.password_hash, password):
                return user
            return None
        except Exception as e:
            logger.error(f"Verify User Error: {e}")
            return None