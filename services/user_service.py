import logging
from werkzeug.security import generate_password_hash, check_password_hash
from models import SessionLocal, User, TokenLog
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from config import TokenConfig

logger = logging.getLogger(__name__)


class UserService:
    @staticmethod
    def create_user(username, password, email=None) -> bool:
        db = SessionLocal()
        try:
            password_hash = generate_password_hash(password)
            # [수정] 신규 유저 생성 시 초기 토큰 지급
            new_user = User(
                id=username,
                password_hash=password_hash,
                email=email,
                token_balance=TokenConfig.INITIAL_TOKEN_BALANCE
            )
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

    # --- [NEW] 토큰 시스템 기능 (1K 토큰 기준 계산) ---

    @staticmethod
    def get_user_balance(user_id):
        """유저의 현재 토큰 잔액 조회"""
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                return user.token_balance
            return 0
        finally:
            db.close()

    @staticmethod
    def calculate_llm_cost(model_name: str, prompt_tokens: int, completion_tokens: int) -> int:
        """
        LLM 토큰 사용량에 따른 비용 정밀 계산
        Config 설정값은 '1,000 토큰' 기준
        """
        # 기본값 설정
        cost_info = TokenConfig.MODEL_COSTS["default"]

        # 모델명 매칭 (대소문자 무시, 부분 일치)
        if model_name:
            model_lower = model_name.lower()
            for key, val in TokenConfig.MODEL_COSTS.items():
                if key in model_lower:
                    cost_info = val
                    break

        # [계산] 1,000 토큰 단위로 나누어 비용 산출
        # 공식: (사용토큰 / 1,000) * 1K당_설정비용
        input_cost = (prompt_tokens / 1000.0) * cost_info["input"]
        output_cost = (completion_tokens / 1000.0) * cost_info["output"]

        # 소수점 버림 (int 형변환)
        total_cost = int(input_cost + output_cost)

        logger.info(f"[COST CALC] Model: {model_name}, Input: {prompt_tokens} tokens, Output: {completion_tokens} tokens")
        logger.info(f"[COST CALC] Cost info: {cost_info}")
        logger.info(f"[COST CALC] Input cost: {input_cost}, Output cost: {output_cost}, Total: {total_cost}")

        return total_cost

    @staticmethod
    def deduct_tokens(user_id, cost, action_type, model_name=None, llm_tokens_used=0) -> int:
        """
        토큰 차감 및 로그 기록 (Atomic Transaction)
        """
        logger.info(f"[TOKEN DEDUCT START] User: {user_id}, Cost: {cost}, Action: {action_type}")
        
        db = SessionLocal()
        try:
            # Row-level locking으로 동시성 문제 방지
            user = db.query(User).filter(User.id == user_id).with_for_update().first()

            if not user:
                logger.error(f"[TOKEN DEDUCT] User not found: {user_id}")
                raise ValueError("User not found")

            logger.info(f"[TOKEN DEDUCT] User found: {user_id}, Current balance: {user.token_balance}")

            # 비용 검증 (무료 모델은 0원일 수 있음)
            if cost > 0 and user.token_balance < cost:
                logger.error(f"[TOKEN DEDUCT] Insufficient tokens: Need {cost}, Have {user.token_balance}")
                raise ValueError(f"토큰이 부족합니다. (필요: {cost}, 보유: {user.token_balance})")

            # 차감
            old_balance = user.token_balance
            user.token_balance -= cost
            new_balance = user.token_balance

            logger.info(f"[TOKEN DEDUCT] Balance update: {old_balance} - {cost} = {new_balance}")

            # 로그 기록
            log = TokenLog(
                user_id=user_id,
                action_type=action_type,
                model_name=model_name,
                tokens_used=llm_tokens_used,
                cost_deducted=cost
            )
            db.add(log)

            db.commit()

            if cost > 0:
                logger.info(f"💰 Token deducted for {user_id}: -{cost} (Action: {action_type}, Model: {model_name})")

            return user.token_balance

        except ValueError as ve:
            db.rollback()
            raise ve
        except Exception as e:
            db.rollback()
            logger.error(f"❌ Token deduction error: {e}")
            raise e
        finally:
            db.close()