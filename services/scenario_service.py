import json
import time
import logging
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime
from sqlalchemy.orm import Session

from config import DEFAULT_PLAYER_VARS
from models import SessionLocal, Scenario, ScenarioHistory, TempScenario

logger = logging.getLogger(__name__)


class ScenarioService:
    """시나리오 DB 관리 서비스"""

    @staticmethod
    def list_scenarios(sort_order: str = 'newest', user_id: str = None, filter_mode: str = 'public',
                       limit: int = None) -> List[Dict[str, Any]]:
        """시나리오 목록 조회 (DB 기반)"""
        db = SessionLocal()
        try:
            query = db.query(Scenario)

            # 필터링 로직
            if filter_mode == 'my' and user_id:
                query = query.filter(Scenario.author_id == user_id)
            elif filter_mode == 'public':
                query = query.filter(Scenario.is_public == True)
            else:  # all
                if user_id:
                    query = query.filter((Scenario.is_public == True) | (Scenario.author_id == user_id))
                else:
                    query = query.filter(Scenario.is_public == True)

            # 정렬 로직
            if sort_order == 'oldest':
                query = query.order_by(Scenario.created_at.asc())
            elif sort_order == 'name_asc':
                query = query.order_by(Scenario.title.asc())
            elif sort_order == 'name_desc':
                query = query.order_by(Scenario.title.desc())
            else:  # newest
                query = query.order_by(Scenario.created_at.desc())

            if limit:
                query = query.limit(limit)

            scenarios = query.all()
            file_infos = []

            for s in scenarios:
                s_data = s.data
                if 'scenario' in s_data:
                    s_data = s_data['scenario']

                p_text = s_data.get('prologue', s_data.get('prologue_text', ''))
                desc = (p_text[:60] + "...") if p_text else "저장된 시나리오"

                file_infos.append({
                    'filename': str(s.id),
                    'id': s.id,
                    'created_time': s.created_at.timestamp() if s.created_at else 0,
                    'title': s.title,
                    'desc': desc,
                    'is_public': s.is_public,
                    'is_owner': (user_id is not None) and (s.author_id == user_id),
                    'author': s.author_id or "System/Anonymous"
                })

            return file_infos
        finally:
            db.close()

    @staticmethod
    def load_scenario(scenario_id: str, user_id: str = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """시나리오 로드 (DB ID 기반)"""
        if not scenario_id:
            return None, "ID 누락"

        db = SessionLocal()
        try:
            db_id = int(scenario_id)
            scenario = db.query(Scenario).filter(Scenario.id == db_id).first()

            if not scenario:
                return None, "시나리오를 찾을 수 없습니다."

            # 접근 권한 체크
            is_accessible = False
            if scenario.is_public:
                is_accessible = True
            elif scenario.author_id is None:
                is_accessible = True
            elif user_id and scenario.author_id == user_id:
                is_accessible = True
            elif user_id:
                is_accessible = True

            if not is_accessible:
                return None, "비공개 시나리오입니다. (접근 권한 없음)"

            full_data = scenario.data
            s_content = full_data.get('scenario', full_data)

            # 시나리오의 variables 필드에서 initial_state 구성
            initial_vars = {}

            # 1. 시나리오의 variables 필드 파싱
            if 'variables' in s_content and isinstance(s_content['variables'], list):
                for var in s_content['variables']:
                    if isinstance(var, dict) and 'name' in var and 'initial_value' in var:
                        var_name = var['name'].lower()
                        initial_vars[var_name] = var['initial_value']

            # 2. 시나리오의 initial_state 필드도 확인 (하위 호환성)
            if 'initial_state' in s_content:
                initial_vars.update(s_content['initial_state'])

            # 3. player_vars도 확인 (하위 호환성)
            if 'player_vars' in full_data:
                for key, value in full_data['player_vars'].items():
                    if key not in initial_vars:
                        initial_vars[key] = value

            # 4. DEFAULT_PLAYER_VARS로 누락된 필드만 채움
            for key, value in DEFAULT_PLAYER_VARS.items():
                if key not in initial_vars:
                    initial_vars[key] = value

            # 5. 시나리오 데이터에 DB ID 추가 (Foreign Key 참조용)
            s_content['id'] = scenario.id

            return {
                'scenario': s_content,
                'player_vars': initial_vars
            }, None

        except ValueError:
            return None, "잘못된 시나리오 ID 형식입니다."
        except Exception as e:
            logger.error(f"Load Error: {e}", exc_info=True)
            return None, str(e)
        finally:
            db.close()

    @staticmethod
    def save_scenario(scenario_json: Dict[str, Any], player_vars: Dict[str, Any] = None, user_id: str = None) -> Tuple[Optional[str], Optional[str]]:
        """시나리오 저장 (DB Insert)"""
        db = SessionLocal()
        try:
            title = scenario_json.get('title', 'Untitled_Scenario')

            if player_vars is None:
                player_vars = DEFAULT_PLAYER_VARS.copy()

            full_data = {
                "scenario": scenario_json,
                "player_vars": player_vars
            }

            is_public_setting = False
            if user_id is None:
                is_public_setting = True

            new_scenario = Scenario(
                title=title,
                author_id=user_id,
                data=full_data,
                is_public=is_public_setting
            )

            db.add(new_scenario)
            db.commit()
            db.refresh(new_scenario)

            return str(new_scenario.id), None

        except Exception as e:
            db.rollback()
            logger.error(f"Save Error: {e}", exc_info=True)
            return None, str(e)
        finally:
            db.close()

    @staticmethod
    def delete_scenario(scenario_id: str, user_id: str) -> Tuple[bool, Optional[str]]:
        """시나리오 삭제"""
        if not scenario_id or not user_id:
            return False, "권한이 없습니다."

        db = SessionLocal()
        try:
            db_id = int(scenario_id)
            scenario = db.query(Scenario).filter(Scenario.id == db_id).first()

            if not scenario:
                return False, "시나리오를 찾을 수 없습니다."

            if scenario.author_id != user_id:
                return False, "삭제 권한이 없습니다."

            # [FIX] 연관된 데이터를 명시적으로 삭제
            # 1. ScenarioHistory 삭제
            db.query(ScenarioHistory).filter(ScenarioHistory.scenario_id == db_id).delete()

            # 2. TempScenario (Draft) 삭제
            db.query(TempScenario).filter(TempScenario.original_scenario_id == db_id).delete()

            # 3. 시나리오 본체 삭제
            db.delete(scenario)
            db.commit()

            logger.info(f"✅ Scenario {db_id} and related data deleted successfully")
            return True, None

        except ValueError:
            return False, "잘못된 ID입니다."
        except Exception as e:
            db.rollback()
            logger.error(f"Delete Error: {e}", exc_info=True)
            return False, str(e)
        finally:
            db.close()

    @staticmethod
    def publish_scenario(scenario_id: str, user_id: str) -> Tuple[bool, Optional[str]]:
        """시나리오 공개 전환"""
        db = SessionLocal()
        try:
            db_id = int(scenario_id)
            scenario = db.query(Scenario).filter(Scenario.id == db_id).first()

            if not scenario:
                return False, "시나리오를 찾을 수 없습니다."

            if scenario.author_id != user_id:
                return False, "권한이 없습니다."

            scenario.is_public = not scenario.is_public
            db.commit()

            status = "공개" if scenario.is_public else "비공개"
            return True, f"{status} 설정 완료"

        except Exception as e:
            db.rollback()
            return False, str(e)
        finally:
            db.close()

    @staticmethod
    def update_scenario(scenario_id: str, updated_data: Dict[str, Any], user_id: str) -> Tuple[bool, Optional[str]]:
        """시나리오 업데이트"""
        if not scenario_id or not user_id:
            return False, "권한이 없습니다."

        db = SessionLocal()
        try:
            db_id = int(scenario_id)
            scenario = db.query(Scenario).filter(Scenario.id == db_id).first()

            if not scenario:
                return False, "시나리오를 찾을 수 없습니다."

            if scenario.author_id != user_id:
                return False, "수정 권한이 없습니다."

            current_data = scenario.data
            current_scenario = current_data.get('scenario', current_data)

            if 'scenes' in updated_data or 'endings' in updated_data or 'prologue' in updated_data:
                for key, value in updated_data.items():
                    current_scenario[key] = value
            else:
                current_scenario = updated_data

            if 'title' in updated_data:
                scenario.title = updated_data['title']

            scenario.data = {
                "scenario": current_scenario,
                "player_vars": current_data.get('player_vars', DEFAULT_PLAYER_VARS.copy())
            }
            scenario.updated_at = datetime.now()

            db.commit()
            return True, None

        except ValueError:
            return False, "잘못된 ID입니다."
        except Exception as e:
            db.rollback()
            logger.error(f"Update Error: {e}", exc_info=True)
            return False, str(e)
        finally:
            db.close()

    @staticmethod
    def get_scenario_for_edit(scenario_id: str, user_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """편집용 시나리오 로드"""
        if not scenario_id or not user_id:
            return None, "권한이 없습니다."

        db = SessionLocal()
        try:
            db_id = int(scenario_id)
            scenario = db.query(Scenario).filter(Scenario.id == db_id).first()

            if not scenario:
                return None, "시나리오를 찾을 수 없습니다."

            if scenario.author_id != user_id:
                return None, "수정 권한이 없습니다."

            full_data = scenario.data
            s_content = full_data.get('scenario', full_data)

            return {
                'id': scenario.id,
                'scenario': s_content,
                'player_vars': full_data.get('player_vars', {}),
                'is_public': scenario.is_public
            }, None

        except ValueError:
            return None, "잘못된 ID입니다."
        except Exception as e:
            logger.error(f"Get for Edit Error: {e}", exc_info=True)
            return None, str(e)
        finally:
            db.close()

    @staticmethod
    def is_recently_created(created_time: float, threshold_seconds: int = 600) -> bool:
        return (time.time() - created_time) < threshold_seconds

    @staticmethod
    def format_time(timestamp: float) -> str:
        if timestamp <= 0: return ""
        dt = datetime.fromtimestamp(timestamp)
        return dt.strftime('%Y-%m-%d %H:%M')

    @staticmethod
    def get_scenario_for_view(scenario_id: int, user_id: Optional[int] = None, db: Session = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        전체 씬 보기용 시나리오 로드 (읽기 전용)
        ✅ [FIX 3] ScenarioService.get_scenario_for_view 메서드 추가
        """
        should_close_db = False
        if db is None:
            db = SessionLocal()
            should_close_db = True

        try:
            scenario = db.query(Scenario).filter(Scenario.id == scenario_id).first()

            if not scenario:
                return None, "시나리오를 찾을 수 없습니다."

            # 접근 권한 체크
            is_accessible = False
            if scenario.is_public:
                is_accessible = True
            elif scenario.author_id is None:
                is_accessible = True
            elif user_id and scenario.author_id == user_id:
                is_accessible = True
            elif user_id:
                is_accessible = True

            if not is_accessible:
                return None, "비공개 시나리오입니다. (접근 권한 없음)"

            full_data = scenario.data
            s_content = full_data.get('scenario', full_data)

            # 시나리오 데이터에 DB ID 추가
            s_content['id'] = scenario.id

            return s_content, None

        except Exception as e:
            logger.error(f"Get Scenario For View Error: {e}", exc_info=True)
            return None, str(e)
        finally:
            if should_close_db:
                db.close()
