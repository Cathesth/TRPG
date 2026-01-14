"""
시나리오 변경 이력 관리 서비스 (History Service)
- Undo/Redo 기능 구현
- 변경 이력 스냅샷 저장/조회
- Railway PostgreSQL 환경에서 영속적 관리
"""
import logging
import copy
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime

# models.py에 정의된 SessionLocal, ScenarioHistory, TempScenario 사용
from models import SessionLocal, ScenarioHistory, TempScenario

logger = logging.getLogger(__name__)

# 최대 이력 저장 개수 (메모리/DB 용량 관리)
MAX_HISTORY_SIZE = 50


class HistoryService:
    """시나리오 변경 이력 관리 서비스"""

    @staticmethod
    def get_session(session_id: str) -> Optional[Dict[str, Any]]:
        """
        세션 ID로 기존 게임 세션 조회
        Returns: PlayerState 딕셔너리 또는 None
        """
        db = SessionLocal()
        try:
            from models import GameSession
            game_session = db.query(GameSession).filter_by(session_key=session_id).first()

            if not game_session:
                logger.warning(f"⚠️ [GET_SESSION] Session not found: {session_id}")
                return None

            # WorldState 복원
            from core.state import WorldState
            world_state_instance = WorldState()
            if game_session.world_state:
                world_state_instance.from_dict(game_session.world_state)

            # PlayerState 복원 (world_state 포함)
            player_state = game_session.player_state.copy()
            player_state['world_state'] = game_session.world_state

            logger.info(f"✅ [GET_SESSION] Session loaded: {session_id}, Scene: {game_session.current_scene_id}")
            return player_state

        except Exception as e:
            logger.error(f"❌ [GET_SESSION] Error: {e}", exc_info=True)
            return None
        finally:
            db.close()

    @staticmethod
    def initialize_history(
        scenario_id: int,
        editor_id: str,
        initial_data: Dict[str, Any]
    ) -> Tuple[bool, Optional[str]]:
        """
        편집 세션 시작 시 초기 이력(Base Snapshot) 생성.
        이미 이력이 존재하면 초기화하지 않음.
        """
        db = SessionLocal()
        try:
            # 기존 이력 확인
            existing = db.query(ScenarioHistory).filter_by(
                scenario_id=scenario_id,
                editor_id=editor_id
            ).first()

            if existing:
                return True, None

            # 새 이력 생성 (Sequence 0)
            history_entry = ScenarioHistory(
                scenario_id=scenario_id,
                editor_id=editor_id,
                action_type='initial',
                action_description='편집 시작',
                snapshot_data=copy.deepcopy(initial_data),
                sequence=0,
                is_current=True,
                created_at=datetime.now()
            )

            db.add(history_entry)
            db.commit()
            return True, None

        except Exception as e:
            db.rollback()
            logger.error(f"History initialize error: {e}", exc_info=True)
            return False, str(e)
        finally:
            db.close()

    @staticmethod
    def add_history(
        scenario_id: int,
        editor_id: str,
        action_type: str,
        action_description: str,
        snapshot_data: Dict[str, Any]
    ) -> Tuple[bool, Optional[str]]:
        """
        새로운 변경 이력 추가 (스냅샷 저장).
        현재 위치 이후의 이력(Redo Stack)은 모두 삭제됨.
        """
        db = SessionLocal()
        try:
            # 현재 활성화된 이력 찾기
            current_entry = db.query(ScenarioHistory).filter_by(
                scenario_id=scenario_id,
                editor_id=editor_id,
                is_current=True
            ).first()

            new_sequence = 0
            if current_entry:
                # 가지치기: 현재 위치 이후의 이력 삭제 (Redo 불가 처리)
                db.query(ScenarioHistory).filter(
                    ScenarioHistory.scenario_id == scenario_id,
                    ScenarioHistory.editor_id == editor_id,
                    ScenarioHistory.sequence > current_entry.sequence
                ).delete(synchronize_session=False)

                # 현재 포인터 이동 준비
                current_entry.is_current = False
                new_sequence = current_entry.sequence + 1

            # 새 이력 생성
            new_entry = ScenarioHistory(
                scenario_id=scenario_id,
                editor_id=editor_id,
                action_type=action_type,
                action_description=action_description,
                snapshot_data=copy.deepcopy(snapshot_data),
                sequence=new_sequence,
                is_current=True,
                created_at=datetime.now()
            )
            db.add(new_entry)

            # 최대 개수 제한: 너무 오래된 이력 삭제
            total_count = db.query(ScenarioHistory).filter_by(
                scenario_id=scenario_id,
                editor_id=editor_id
            ).count()

            if total_count > MAX_HISTORY_SIZE:
                # sequence가 가장 낮은(오래된) 항목 삭제
                # (주의: 0번 초기 상태는 보존하는 게 좋지만, 로직 단순화를 위해 일단 삭제 허용)
                limit = total_count - MAX_HISTORY_SIZE
                subquery = db.query(ScenarioHistory.id).filter_by(
                    scenario_id=scenario_id,
                    editor_id=editor_id
                ).order_by(ScenarioHistory.sequence.asc()).limit(limit).subquery()

                db.query(ScenarioHistory).filter(ScenarioHistory.id.in_(subquery)).delete(synchronize_session=False)

            db.commit()
            return True, None

        except Exception as e:
            db.rollback()
            logger.error(f"History add error: {e}", exc_info=True)
            return False, str(e)
        finally:
            db.close()

    @staticmethod
    def get_history_list(
        scenario_id: int,
        editor_id: str
    ) -> Tuple[List[Dict[str, Any]], int, Optional[str]]:
        """
        전체 이력 목록 조회
        Returns: (history_list, current_sequence, error)
        """
        db = SessionLocal()
        try:
            entries = db.query(ScenarioHistory).filter_by(
                scenario_id=scenario_id,
                editor_id=editor_id
            ).order_by(ScenarioHistory.sequence.desc()).all()

            current_sequence = -1
            history_list = []

            for entry in entries:
                if entry.is_current:
                    current_sequence = entry.sequence

                # to_dict() 메서드가 없다면 수동 변환
                item = {
                    "id": entry.id,
                    "action_type": entry.action_type,
                    "action_description": entry.action_description,
                    "sequence": entry.sequence,
                    "is_current": entry.is_current,
                    "created_at": entry.created_at.timestamp() if entry.created_at else 0
                }
                history_list.append(item)

            return history_list, current_sequence, None

        except Exception as e:
            logger.error(f"Get history list error: {e}", exc_info=True)
            return [], -1, str(e)
        finally:
            db.close()

    @staticmethod
    def get_undo_redo_status(
        scenario_id: int,
        editor_id: str
    ) -> Dict[str, Any]:
        """Undo/Redo 가능 여부 확인"""
        db = SessionLocal()
        try:
            current_entry = db.query(ScenarioHistory).filter_by(
                scenario_id=scenario_id,
                editor_id=editor_id,
                is_current=True
            ).first()

            if not current_entry:
                return {'can_undo': False, 'can_redo': False, 'current_sequence': -1}

            # Undo 가능: 현재 시퀀스가 DB의 최소 시퀀스보다 큰지 확인 (혹은 0보다 큰지)
            min_seq = db.query(db.func.min(ScenarioHistory.sequence)).filter_by(
                scenario_id=scenario_id, editor_id=editor_id
            ).scalar() or 0

            can_undo = current_entry.sequence > min_seq

            # Redo 가능: 현재 시퀀스보다 큰 시퀀스가 존재하는지 확인
            next_entry = db.query(ScenarioHistory).filter(
                ScenarioHistory.scenario_id == scenario_id,
                ScenarioHistory.editor_id == editor_id,
                ScenarioHistory.sequence == current_entry.sequence + 1
            ).first()

            can_redo = next_entry is not None

            return {
                'can_undo': can_undo,
                'can_redo': can_redo,
                'current_sequence': current_entry.sequence
            }
        except Exception as e:
            logger.error(f"Status check error: {e}")
            return {'can_undo': False, 'can_redo': False, 'current_sequence': -1}
        finally:
            db.close()

    @staticmethod
    def undo(
        scenario_id: int,
        editor_id: str
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Undo 실행: 이전 상태로 되돌리고 Draft 업데이트"""
        db = SessionLocal()
        try:
            current = db.query(ScenarioHistory).filter_by(
                scenario_id=scenario_id, editor_id=editor_id, is_current=True
            ).first()

            if not current:
                return None, "이력이 없습니다."

            # 이전 이력 찾기
            prev = db.query(ScenarioHistory).filter_by(
                scenario_id=scenario_id, editor_id=editor_id, sequence=current.sequence - 1
            ).first()

            if not prev:
                return None, "더 이상 되돌릴 수 없습니다."

            # 포인터 이동
            current.is_current = False
            prev.is_current = True

            # Draft 데이터 롤백
            draft = db.query(TempScenario).filter_by(
                original_scenario_id=scenario_id, editor_id=editor_id
            ).first()

            restored_data = copy.deepcopy(prev.snapshot_data)

            if draft:
                draft.data = restored_data
                draft.updated_at = datetime.now()
            else:
                # Draft가 없으면 새로 생성 (방어 코드)
                draft = TempScenario(
                    original_scenario_id=scenario_id,
                    editor_id=editor_id,
                    data=restored_data
                )
                db.add(draft)

            db.commit()
            return restored_data, None

        except Exception as e:
            db.rollback()
            logger.error(f"Undo error: {e}", exc_info=True)
            return None, str(e)
        finally:
            db.close()

    @staticmethod
    def redo(
        scenario_id: int,
        editor_id: str
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Redo 실행: 다음 상태로 되돌리고 Draft 업데이트"""
        db = SessionLocal()
        try:
            current = db.query(ScenarioHistory).filter_by(
                scenario_id=scenario_id, editor_id=editor_id, is_current=True
            ).first()

            if not current:
                return None, "이력이 없습니다."

            # 다음 이력 찾기
            next_entry = db.query(ScenarioHistory).filter_by(
                scenario_id=scenario_id, editor_id=editor_id, sequence=current.sequence + 1
            ).first()

            if not next_entry:
                return None, "다시 실행할 내용이 없습니다."

            # 포인터 이동
            current.is_current = False
            next_entry.is_current = True

            # Draft 데이터 롤포워드
            draft = db.query(TempScenario).filter_by(
                original_scenario_id=scenario_id, editor_id=editor_id
            ).first()

            restored_data = copy.deepcopy(next_entry.snapshot_data)

            if draft:
                draft.data = restored_data
                draft.updated_at = datetime.now()
            else:
                draft = TempScenario(
                    original_scenario_id=scenario_id,
                    editor_id=editor_id,
                    data=restored_data
                )
                db.add(draft)

            db.commit()
            return restored_data, None

        except Exception as e:
            db.rollback()
            logger.error(f"Redo error: {e}", exc_info=True)
            return None, str(e)
        finally:
            db.close()

    @staticmethod
    def restore_to_point(
        scenario_id: int,
        editor_id: str,
        history_id: int
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """특정 이력 시점으로 점프 (복원)"""
        db = SessionLocal()
        try:
            target = db.query(ScenarioHistory).filter_by(
                id=history_id, scenario_id=scenario_id, editor_id=editor_id
            ).first()

            if not target:
                return None, "해당 이력을 찾을 수 없습니다."

            # 기존 current 해제
            db.query(ScenarioHistory).filter_by(
                scenario_id=scenario_id, editor_id=editor_id, is_current=True
            ).update({'is_current': False})

            # target을 current로 설정
            target.is_current = True

            # Draft 업데이트
            draft = db.query(TempScenario).filter_by(
                original_scenario_id=scenario_id, editor_id=editor_id
            ).first()

            restored_data = copy.deepcopy(target.snapshot_data)

            if draft:
                draft.data = restored_data
                draft.updated_at = datetime.now()
            else:
                db.add(TempScenario(
                    original_scenario_id=scenario_id,
                    editor_id=editor_id,
                    data=restored_data
                ))

            db.commit()
            return restored_data, None

        except Exception as e:
            db.rollback()
            logger.error(f"Restore error: {e}", exc_info=True)
            return None, str(e)
        finally:
            db.close()

    @staticmethod
    def clear_history(
        scenario_id: int,
        editor_id: str
    ) -> Tuple[bool, Optional[str]]:
        """이력 전체 삭제 (초기화)"""
        db = SessionLocal()
        try:
            db.query(ScenarioHistory).filter_by(
                scenario_id=scenario_id, editor_id=editor_id
            ).delete(synchronize_session=False)
            db.commit()
            return True, None
        except Exception as e:
            db.rollback()
            logger.error(f"Clear history error: {e}")
            return False, str(e)
        finally:
            db.close()