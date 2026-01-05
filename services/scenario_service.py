import json
import time
import logging
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime

from config import DEFAULT_PLAYER_VARS
from models import db, Scenario

logger = logging.getLogger(__name__)

class ScenarioService:
    """시나리오 DB 관리 서비스"""

    @staticmethod
    def list_scenarios(sort_order: str = 'newest', user_id: str = None, filter_mode: str = 'public') -> List[Dict[str, Any]]:
        """
        시나리오 목록 조회 (DB 기반)
        """
        query = Scenario.query

        # 필터링 로직
        if filter_mode == 'my' and user_id:
            query = query.filter_by(author_id=user_id)
        elif filter_mode == 'public':
            query = query.filter_by(is_public=True)
        else: # all (공개 + 내 것)
            if user_id:
                query = query.filter((Scenario.is_public == True) | (Scenario.author_id == user_id))
            else:
                query = query.filter_by(is_public=True)

        # 정렬 로직
        if sort_order == 'oldest':
            query = query.order_by(Scenario.created_at.asc())
        elif sort_order == 'name_asc':
            query = query.order_by(Scenario.title.asc())
        elif sort_order == 'name_desc':
            query = query.order_by(Scenario.title.desc())
        else:  # newest
            query = query.order_by(Scenario.created_at.desc())

        scenarios = query.all()
        file_infos = []

        for s in scenarios:
            s_data = s.data
            # DB 데이터 구조 호환성 체크
            if 'scenario' in s_data:
                s_data = s_data['scenario']
            
            p_text = s_data.get('prologue', s_data.get('prologue_text', ''))
            desc = (p_text[:60] + "...") if p_text else "저장된 시나리오"

            file_infos.append({
                'filename': str(s.id), # DB ID를 filename처럼 사용
                'id': s.id,
                'created_time': s.created_at.timestamp(),
                'title': s.title,
                'desc': desc,
                'is_public': s.is_public,
                'is_owner': (user_id is not None) and (s.author_id == user_id),
                'author': s.author_id or "System"
            })

        return file_infos

    @staticmethod
    def load_scenario(scenario_id: str, user_id: str = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        시나리오 로드 (DB ID 기반)
        """
        if not scenario_id:
            return None, "ID 누락"

        try:
            # 숫자형 ID 변환
            db_id = int(scenario_id)
            scenario = Scenario.query.get(db_id)

            if not scenario:
                return None, "시나리오를 찾을 수 없습니다."

            # 권한 체크 (비공개이고 내 것도 아니면 접근 불가)
            if not scenario.is_public:
                if not user_id or scenario.author_id != user_id:
                    return None, "접근 권한이 없습니다."

            full_data = scenario.data
            s_content = full_data.get('scenario', full_data)
            initial_vars = full_data.get('player_vars', s_content.get('initial_state', {}))

            # 기본값 보장
            for key, value in DEFAULT_PLAYER_VARS.items():
                if key not in initial_vars:
                    initial_vars[key] = value

            return {
                'scenario': s_content,
                'player_vars': initial_vars
            }, None

        except ValueError:
            return None, "잘못된 시나리오 ID 형식입니다."
        except Exception as e:
            logger.error(f"Load Error: {e}", exc_info=True)
            return None, str(e)

    @staticmethod
    def save_scenario(scenario_json: Dict[str, Any], player_vars: Dict[str, Any] = None, user_id: str = None) -> Tuple[Optional[str], Optional[str]]:
        """
        시나리오 저장 (DB Insert/Update)
        """
        try:
            title = scenario_json.get('title', 'Untitled_Scenario')
            
            if player_vars is None:
                player_vars = DEFAULT_PLAYER_VARS.copy()

            full_data = {
                "scenario": scenario_json,
                "player_vars": player_vars
            }

            # 신규 생성
            new_scenario = Scenario(
                title=title,
                author_id=user_id,
                data=full_data,
                is_public=False # 기본 비공개
            )
            
            db.session.add(new_scenario)
            db.session.commit()

            return str(new_scenario.id), None

        except Exception as e:
            db.session.rollback()
            logger.error(f"Save Error: {e}", exc_info=True)
            return None, str(e)

    @staticmethod
    def delete_scenario(scenario_id: str, user_id: str) -> Tuple[bool, Optional[str]]:
        """시나리오 삭제"""
        if not scenario_id or not user_id:
            return False, "권한이 없습니다."

        try:
            db_id = int(scenario_id)
            scenario = Scenario.query.get(db_id)

            if not scenario:
                return False, "시나리오를 찾을 수 없습니다."
            
            if scenario.author_id != user_id:
                return False, "삭제 권한이 없습니다."

            db.session.delete(scenario)
            db.session.commit()
            return True, None
            
        except ValueError:
            return False, "잘못된 ID입니다."
        except Exception as e:
            db.session.rollback()
            return False, str(e)

    @staticmethod
    def publish_scenario(scenario_id: str, user_id: str) -> Tuple[bool, Optional[str]]:
        """시나리오 공개 전환"""
        try:
            db_id = int(scenario_id)
            scenario = Scenario.query.get(db_id)

            if not scenario:
                return False, "시나리오를 찾을 수 없습니다."
            
            if scenario.author_id != user_id:
                return False, "권한이 없습니다."

            # 토글 방식 (공개 <-> 비공개)
            scenario.is_public = not scenario.is_public
            db.session.commit()
            
            status = "공개" if scenario.is_public else "비공개"
            return True, f"{status} 설정 완료"

        except Exception as e:
            db.session.rollback()
            return False, str(e)

    @staticmethod
    def is_recently_created(created_time: float, threshold_seconds: int = 600) -> bool:
        return (time.time() - created_time) < threshold_seconds

    @staticmethod
    def format_time(timestamp: float) -> str:
        if timestamp <= 0: return ""
        dt = datetime.fromtimestamp(timestamp)
        return dt.strftime('%Y-%m-%d %H:%M')