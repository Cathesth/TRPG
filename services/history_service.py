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

from models import SessionLocal, ScenarioHistory, Scenario, TempScenario

logger = logging.getLogger(__name__)

# 최대 이력 저장 개수 (메모리 절약)
MAX_HISTORY_SIZE = 50


class HistoryService:
    """시나리오 변경 이력 관리 서비스"""

    @staticmethod
    def initialize_history(
        scenario_id: int,
        editor_id: str,
        initial_data: Dict[str, Any]
    ) -> Tuple[bool, Optional[str]]:
        """
        편집 세션 시작 시 초기 이력 생성
        이미 이력이 있으면 현재 위치만 업데이트

        Args:
            scenario_id: 시나리오 ID
            editor_id: 편집자 ID
            initial_data: 초기 시나리오 데이터

        Returns:
            (success, error_message)
        """
        db = SessionLocal()
        try:
            # 기존 이력 확인
            existing = db.query(ScenarioHistory).filter_by(
                scenario_id=scenario_id,
                editor_id=editor_id
            ).first()

            if existing:
                # 기존 이력이 있으면 초기화하지 않음
                return True, None

            # 새 이력 생성 (초기 상태)
            history_entry = ScenarioHistory(
                scenario_id=scenario_id,
                editor_id=editor_id,
                action_type='initial',
                action_description='편집 시작',
                snapshot_data=copy.deepcopy(initial_data),
                sequence=0,
                is_current=True
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
        새 변경 이력 추가
        현재 위치 이후의 이력은 삭제 (Redo 스택 초기화)

        Args:
            scenario_id: 시나리오 ID
            editor_id: 편집자 ID
            action_type: 작업 유형 ('scene_edit', 'scene_add', 'scene_delete', 'ending_edit', 'reorder', 'prologue_edit')
            action_description: 작업 설명
            snapshot_data: 현재 시점의 전체 시나리오 데이터

        Returns:
            (success, error_message)
        """
        db = SessionLocal()
        try:
            # 현재 위치 찾기
            current_entry = db.query(ScenarioHistory).filter_by(
                scenario_id=scenario_id,
                editor_id=editor_id,
                is_current=True
            ).first()

            if current_entry:
                # 현재 위치 이후의 이력 삭제 (Redo 스택 초기화)
                db.query(ScenarioHistory).filter(
                    ScenarioHistory.scenario_id == scenario_id,
                    ScenarioHistory.editor_id == editor_id,
                    ScenarioHistory.sequence > current_entry.sequence
                ).delete()

                # 현재 위치 해제
                current_entry.is_current = False
                new_sequence = current_entry.sequence + 1
            else:
                new_sequence = 0

            # 새 이력 추가
            new_entry = ScenarioHistory(
                scenario_id=scenario_id,
                editor_id=editor_id,
                action_type=action_type,
                action_description=action_description,
                snapshot_data=copy.deepcopy(snapshot_data),
                sequence=new_sequence,
                is_current=True
            )

            db.add(new_entry)

            # 이력 개수 제한 (오래된 이력 삭제)
            total_count = db.query(ScenarioHistory).filter_by(
                scenario_id=scenario_id,
                editor_id=editor_id
            ).count()

            if total_count > MAX_HISTORY_SIZE:
                # 가장 오래된 이력들 삭제
                oldest_entries = db.query(ScenarioHistory).filter_by(
                    scenario_id=scenario_id,
                    editor_id=editor_id
                ).order_by(ScenarioHistory.sequence.asc()).limit(total_count - MAX_HISTORY_SIZE).all()

                for entry in oldest_entries:
                    db.delete(entry)

            db.commit()

            return True, None

        except Exception as e:
            db.rollback()
            logger.error(f"History add error: {e}", exc_info=True)
            return False, str(e)
        finally:
            db.close()

    @staticmethod
    def undo(
        scenario_id: int,
        editor_id: str
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        Undo 수행 - 이전 상태로 복원

        Args:
            scenario_id: 시나리오 ID
            editor_id: 편집자 ID

        Returns:
            (복원된 시나리오 데이터, error_message)
        """
        db = SessionLocal()
        try:
            # 현재 위치 찾기
            current_entry = db.query(ScenarioHistory).filter_by(
                scenario_id=scenario_id,
                editor_id=editor_id,
                is_current=True
            ).first()

            if not current_entry:
                return None, "이력이 없습니다."

            if current_entry.sequence <= 0:
                return None, "더 이상 되돌릴 수 없습니다."

            # 이전 이력 찾기
            prev_entry = db.query(ScenarioHistory).filter_by(
                scenario_id=scenario_id,
                editor_id=editor_id,
                sequence=current_entry.sequence - 1
            ).first()

            if not prev_entry:
                return None, "이전 이력을 찾을 수 없습니다."

            # 현재 위치 변경
            current_entry.is_current = False
            prev_entry.is_current = True

            # Draft도 업데이트
            draft = db.query(TempScenario).filter_by(
                original_scenario_id=scenario_id,
                editor_id=editor_id
            ).first()

            if draft:
                draft.data = copy.deepcopy(prev_entry.snapshot_data)
                draft.updated_at = datetime.utcnow()

            db.commit()

            return prev_entry.snapshot_data, None

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
        """
        Redo 수행 - 다음 상태로 복원

        Args:
            scenario_id: 시나리오 ID
            editor_id: 편집자 ID

        Returns:
            (복원된 시나리오 데이터, error_message)
        """
        db = SessionLocal()
        try:
            # 현재 위치 찾기
            current_entry = db.query(ScenarioHistory).filter_by(
                scenario_id=scenario_id,
                editor_id=editor_id,
                is_current=True
            ).first()

            if not current_entry:
                return None, "이력이 없습니다."

            # 다음 이력 찾기
            next_entry = db.query(ScenarioHistory).filter_by(
                scenario_id=scenario_id,
                editor_id=editor_id,
                sequence=current_entry.sequence + 1
            ).first()

            if not next_entry:
                return None, "더 이상 다시 실행할 수 없습니다."

            # 현재 위치 변경
            current_entry.is_current = False
            next_entry.is_current = True

            # Draft도 업데이트
            draft = db.query(TempScenario).filter_by(
                original_scenario_id=scenario_id,
                editor_id=editor_id
            ).first()

            if draft:
                draft.data = copy.deepcopy(next_entry.snapshot_data)
                draft.updated_at = datetime.utcnow()

            db.commit()

            return next_entry.snapshot_data, None

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
        """
        특정 이력 시점으로 복원

        Args:
            scenario_id: 시나리오 ID
            editor_id: 편집자 ID
            history_id: 복원할 이력 ID

        Returns:
            (복원된 시나리오 데이터, error_message)
        """
        db = SessionLocal()
        try:
            # 대상 이력 찾기
            target_entry = db.query(ScenarioHistory).filter_by(
                id=history_id,
                scenario_id=scenario_id,
                editor_id=editor_id
            ).first()

            if not target_entry:
                return None, "이력을 찾을 수 없습니다."

            # 현재 위치 해제
            db.query(ScenarioHistory).filter_by(
                scenario_id=scenario_id,
                editor_id=editor_id,
                is_current=True
            ).update({'is_current': False})

            # 대상 이력을 현재 위치로 설정
            target_entry.is_current = True

            # Draft도 업데이트
            draft = db.query(TempScenario).filter_by(
                original_scenario_id=scenario_id,
                editor_id=editor_id
            ).first()

            if draft:
                draft.data = copy.deepcopy(target_entry.snapshot_data)
                draft.updated_at = datetime.utcnow()

            db.commit()

            return target_entry.snapshot_data, None

        except Exception as e:
            db.rollback()
            logger.error(f"Restore error: {e}", exc_info=True)
            return None, str(e)
        finally:
            db.close()

    @staticmethod
    def get_history_list(
        scenario_id: int,
        editor_id: str
    ) -> Tuple[List[Dict[str, Any]], int, Optional[str]]:
        """
        이력 목록 조회

        Args:
            scenario_id: 시나리오 ID
            editor_id: 편집자 ID

        Returns:
            (이력 목록, 현재 위치 sequence, error_message)
        """
        db = SessionLocal()
        try:
            entries = db.query(ScenarioHistory).filter_by(
                scenario_id=scenario_id,
                editor_id=editor_id
            ).order_by(ScenarioHistory.sequence.desc()).all()

            current_sequence = -1
            for entry in entries:
                if entry.is_current:
                    current_sequence = entry.sequence
                    break

            history_list = [entry.to_dict() for entry in entries]

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
        """
        Undo/Redo 가능 상태 조회

        Args:
            scenario_id: 시나리오 ID
            editor_id: 편집자 ID

        Returns:
            {'can_undo': bool, 'can_redo': bool, 'current_sequence': int, 'total_count': int}
        """
        db = SessionLocal()
        try:
            current_entry = db.query(ScenarioHistory).filter_by(
                scenario_id=scenario_id,
                editor_id=editor_id,
                is_current=True
            ).first()

            if not current_entry:
                return {
                    'can_undo': False,
                    'can_redo': False,
                    'current_sequence': -1,
                    'total_count': 0
                }

            # Undo 가능 여부 (현재 위치가 0보다 크면 가능)
            can_undo = current_entry.sequence > 0

            # Redo 가능 여부 (현재 위치 이후에 이력이 있으면 가능)
            next_exists = db.query(ScenarioHistory).filter_by(
                scenario_id=scenario_id,
                editor_id=editor_id,
                sequence=current_entry.sequence + 1
            ).first() is not None

            total_count = db.query(ScenarioHistory).filter_by(
                scenario_id=scenario_id,
                editor_id=editor_id
            ).count()

            return {
                'can_undo': can_undo,
                'can_redo': next_exists,
                'current_sequence': current_entry.sequence,
                'total_count': total_count
            }

        except Exception as e:
            logger.error(f"Get undo/redo status error: {e}", exc_info=True)
            return {
                'can_undo': False,
                'can_redo': False,
                'current_sequence': -1,
                'total_count': 0
            }
        finally:
            db.close()

    @staticmethod
    def clear_history(
        scenario_id: int,
        editor_id: str
    ) -> Tuple[bool, Optional[str]]:
        """
        이력 전체 삭제 (편집 완료 또는 취소 시)

        Args:
            scenario_id: 시나리오 ID
            editor_id: 편집자 ID

        Returns:
            (success, error_message)
        """
        db = SessionLocal()
        try:
            db.query(ScenarioHistory).filter_by(
                scenario_id=scenario_id,
                editor_id=editor_id
            ).delete()

            db.commit()
            return True, None

        except Exception as e:
            db.rollback()
            logger.error(f"Clear history error: {e}", exc_info=True)
            return False, str(e)
        finally:
            db.close()
