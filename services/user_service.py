import logging
from werkzeug.security import generate_password_hash, check_password_hash
from models import SessionLocal, User
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

class UserService:
    @staticmethod
    def create_user(username, password, email=None) -> bool:
        db = SessionLocal()
        try:
            password_hash = generate_password_hash(password)
            new_user = User(id=username, password_hash=password_hash, email=email)
            db.add(new_user)
            db.commit()
            return True
        except IntegrityError:
            db.rollback()
            return False
        except Exception as e:
            logger.error(f"Create User Error: {e}")
            db.rollback()
            return False
        finally:
            db.close()

    @staticmethod
    def verify_user(username, password):
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == username).first()
            if user and check_password_hash(user.password_hash, password):
                return user
            return None
        except Exception as e:
            logger.error(f"Verify User Error: {e}")
            return None
        finally:
            db.close()
