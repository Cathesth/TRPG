"""
Draft 시스템 서비스
- 임시 시나리오 저장/로드/최종 반영
- ID 순차 재정렬 및 참조 동기화
- 삭제 안전장치 및 데이터 무결성 관리
- 특수문자 이스케이프 처리
"""
import logging
import re
from typing import Dict, Any, List, Optional, Tuple, Set
from datetime import datetime
from collections import deque

from models import SessionLocal, Scenario, TempScenario
from config import DEFAULT_PLAYER_VARS
# core.utils가 없다면 내부적으로 간단한 검증 로직을 사용할 수 있습니다.
# 여기서는 import가 가능하다고 가정합니다.
try:
    from core.utils import can_publish_scenario
except ImportError:
    # Fallback if core.utils is not available
    def can_publish_scenario(data):
        return True, None

logger = logging.getLogger(__name__)


class DraftService:
    """Draft 시스템 서비스"""

    # Mermaid 문법 파손 방지용 특수문자 이스케이프 매핑
    ESCAPE_MAP = {
        '(': '&#40;',
        ')': '&#41;',
        '[': '&#91;',
        ']': '&#93;',
        '"': '&quot;',
        "'": '&#39;',
        '<': '&lt;',
        '>': '&gt;',
        '{': '&#123;',
        '}': '&#125;',
        '|': '&#124;',
        '#': '&#35;',
    }

    @staticmethod
    def escape_for_mermaid(text: str) -> str:
        """Mermaid 문법 파손 방지를 위한 특수문자 이스케이프"""
        if not text:
            return ''
        if not isinstance(text, str):
            return str(text)
        result = text
        for char, escape in DraftService.ESCAPE_MAP.items():
            result = result.replace(char, escape)
        return result

    @staticmethod
    def sanitize_scenario_data(scenario_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        시나리오 데이터의 텍스트 필드를 이스케이프 처리 (저장 전 수행 권장)
        """
        if not scenario_data:
            return {}

        sanitized = scenario_data.copy()

        # 프롤로그
        if 'prologue' in sanitized:
            sanitized['prologue'] = DraftService.escape_for_mermaid(sanitized['prologue'])

        # 씬
        if 'scenes' in sanitized:
            for scene in sanitized['scenes']:
                if 'title' in scene:
                    scene['title'] = DraftService.escape_for_mermaid(scene['title'])
                if 'transitions' in scene:
                    for trans in scene['transitions']:
                        if 'trigger' in trans:
                            trans['trigger'] = DraftService.escape_for_mermaid(trans['trigger'])

        # 엔딩
        if 'endings' in sanitized:
            for ending in sanitized['endings']:
                if 'title' in ending:
                    ending['title'] = DraftService.escape_for_mermaid(ending['title'])

        return sanitized

    # ==========================
    #  DB Operations (CRUD)
    # ==========================

    @staticmethod
    def get_draft(scenario_id: int, user_id: str) -> Tuple[Dict[str, Any], Optional[str]]:
        """
        Draft 조회. 없으면 원본 시나리오 반환.
        Returns: ({scenario: dict, is_draft: bool, ...}, error_message)
        """
        db = SessionLocal()
        try:
            # 1. 원본 존재 확인
            origin = db.query(Scenario).filter(Scenario.id == scenario_id).first()
            if not origin:
                return {}, "시나리오를 찾을 수 없습니다."

            # 권한 체크 (소유자만 Draft 접근 가능)
            if origin.author_id != user_id:
                return {}, "접근 권한이 없습니다."

            # 2. Draft 확인
            draft = db.query(TempScenario).filter(
                TempScenario.original_scenario_id == scenario_id,
                TempScenario.editor_id == user_id
            ).first()

            if draft:
                return {
                    'scenario': draft.data,
                    'is_draft': True,
                    'updated_at': draft.updated_at.isoformat() if draft.updated_at else None
                }, None
            else:
                # Draft 없으면 원본 데이터 반환
                origin_data = origin.data.get('scenario', origin.data)
                return {
                    'scenario': origin_data,
                    'is_draft': False,
                    'updated_at': origin.updated_at.isoformat() if origin.updated_at else None
                }, None
        except Exception as e:
            logger.error(f"Get draft error: {e}")
            return {}, str(e)
        finally:
            db.close()

    @staticmethod
    def create_or_update_draft(scenario_id: int, user_id: str, data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Draft 생성 또는 업데이트 (내부 호출용)"""
        db = SessionLocal()
        try:
            draft = db.query(TempScenario).filter(
                TempScenario.original_scenario_id == scenario_id,
                TempScenario.editor_id == user_id
            ).first()

            # 데이터 정제 (이스케이프 등 필요시 수행, 현재는 원본 저장)
            # sanitized_data = DraftService.sanitize_scenario_data(data)

            if draft:
                draft.data = data
                draft.updated_at = datetime.now()
            else:
                draft = TempScenario(
                    original_scenario_id=scenario_id,
                    editor_id=user_id,
                    data=data
                )
                db.add(draft)

            db.commit()
            return True, None
        except Exception as e:
            db.rollback()
            logger.error(f"Save draft error: {e}")
            return False, str(e)
        finally:
            db.close()

    @staticmethod
    def save_draft(scenario_id: int, user_id: str, data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """API용 Draft 저장 래퍼"""
        return DraftService.create_or_update_draft(scenario_id, user_id, data)

    @staticmethod
    def publish_draft(scenario_id: int, user_id: str, force: bool = False) -> Tuple[bool, Optional[str], Optional[Dict]]:
        """
        Draft 내용을 원본 시나리오에 덮어쓰기 (최종 반영)
        Returns: (success, error, validation_result)
        """
        db = SessionLocal()
        try:
            draft = db.query(TempScenario).filter(
                TempScenario.original_scenario_id == scenario_id,
                TempScenario.editor_id == user_id
            ).first()

            if not draft:
                return False, "저장된 Draft가 없습니다.", None

            # 유효성 검사
            can_publish, validation = can_publish_scenario(draft.data)

            if not can_publish and not force:
                return False, "유효성 검사 실패 (강제 반영하려면 force=true 필요)", validation.to_dict() if validation else None

            # 원본 업데이트
            origin = db.query(Scenario).filter(Scenario.id == scenario_id).first()
            if not origin:
                return False, "원본 시나리오가 삭제되었습니다.", None

            # 기존 player_vars 등은 유지하고 scenario 키만 업데이트
            current_db_data = origin.data or {}
            new_data = {
                "scenario": draft.data,
                "player_vars": current_db_data.get("player_vars", DEFAULT_PLAYER_VARS.copy())
            }

            # 제목 등 메타데이터 동기화
            if 'title' in draft.data:
                origin.title = draft.data['title']

            origin.data = new_data
            origin.updated_at = datetime.now()

            # 반영 후 Draft 삭제
            db.delete(draft)
            db.commit()

            return True, None, validation.to_dict() if validation else None
        except Exception as e:
            db.rollback()
            logger.error(f"Publish draft error: {e}")
            return False, str(e), None
        finally:
            db.close()

    @staticmethod
    def discard_draft(scenario_id: int, user_id: str) -> Tuple[bool, Optional[str]]:
        """Draft 삭제 (변경사항 취소)"""
        db = SessionLocal()
        try:
            db.query(TempScenario).filter(
                TempScenario.original_scenario_id == scenario_id,
                TempScenario.editor_id == user_id
            ).delete()
            db.commit()
            return True, None
        except Exception as e:
            db.rollback()
            return False, str(e)
        finally:
            db.close()

    # ==========================
    #  Data Manipulation (Pure)
    # ==========================

    @staticmethod
    def reorder_scene_ids(scenario_data: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
        """
        BFS로 그래프를 순회하며 scene_1, scene_2... 형태로 ID 재발급.
        Returns: (updated_scenario_data, id_mapping)
        """
        scenes = scenario_data.get('scenes', [])
        if not scenes:
            return scenario_data, {}

        # 1. 씬 맵핑 준비
        scene_map = {s['scene_id']: s for s in scenes}

        # 2. 진입점(Root) 찾기
        # 프롤로그에서 연결되는 씬들 우선
        roots = []
        if scenario_data.get('prologue_connects_to'):
            roots.extend(scenario_data['prologue_connects_to'])

        # 참조되지 않는 씬들(고립된 시작점) 찾기
        referenced_ids = set()
        for s in scenes:
            for t in s.get('transitions', []):
                if t.get('target_scene_id'):
                    referenced_ids.add(t['target_scene_id'])

        for s in scenes:
            sid = s['scene_id']
            if sid not in referenced_ids and sid not in roots:
                roots.append(sid)

        # 3. BFS 순회
        queue = deque(roots)
        visited_order = []
        visited_set = set()

        while queue:
            curr_id = queue.popleft()
            if curr_id in visited_set or curr_id not in scene_map:
                continue

            visited_set.add(curr_id)
            visited_order.append(curr_id)

            # 자식 노드 큐에 추가
            scene = scene_map[curr_id]
            for t in scene.get('transitions', []):
                tgt = t.get('target_scene_id')
                if tgt and tgt not in visited_set:
                    queue.append(tgt)

        # 방문 안 된 씬들(완전 고립) 뒤에 추가
        for s in scenes:
            if s['scene_id'] not in visited_set:
                visited_order.append(s['scene_id'])

        # 4. 새 ID 부여 및 매핑 생성
        id_mapping = {}
        for idx, old_id in enumerate(visited_order):
            new_id = f"scene_{idx + 1}"
            if old_id != new_id:
                id_mapping[old_id] = new_id

        if not id_mapping:
            return scenario_data, {}

        # 5. 데이터 업데이트 (Deep Copy 권장하지만 성능상 직접 수정 후 리턴)
        # 여기서는 안전하게 새 리스트 생���
        new_scenes = []
        for s in scenes:
            new_s = s.copy()
            # ID 변경
            old_id = s['scene_id']
            if old_id in id_mapping:
                new_s['scene_id'] = id_mapping[old_id]

            # Transition 타겟 변경
            new_trans = []
            for t in s.get('transitions', []):
                nt = t.copy()
                tid = t.get('target_scene_id')
                if tid in id_mapping:
                    nt['target_scene_id'] = id_mapping[tid]
                new_trans.append(nt)
            new_s['transitions'] = new_trans
            new_scenes.append(new_s)

        # 데이터 구조 갱신
        updated_data = scenario_data.copy()
        updated_data['scenes'] = new_scenes

        # 프롤로그 연결 갱신
        if 'prologue_connects_to' in updated_data:
            updated_data['prologue_connects_to'] = [
                id_mapping.get(pid, pid) for pid in updated_data['prologue_connects_to']
            ]

        return updated_data, id_mapping

    @staticmethod
    def check_scene_references(scenario_data: Dict[str, Any], target_id: str) -> List[Dict[str, Any]]:
        """
        특정 씬(target_id)을 참조하고 있는 모든 씬/전환 정보를 반환
        """
        refs = []
        scenes = scenario_data.get('scenes', [])

        # 1. 씬에서의 참조
        for s in scenes:
            if s['scene_id'] == target_id:
                continue
            for idx, t in enumerate(s.get('transitions', [])):
                if t.get('target_scene_id') == target_id:
                    refs.append({
                        'type': 'scene',
                        'from_id': s['scene_id'],
                        'from_title': s.get('title', s['scene_id']),
                        'trigger': t.get('trigger', '조건 없음'),
                        'index': idx
                    })

        # 2. 프롤로그에서의 참조 (구조에 따라 다름)
        if target_id in scenario_data.get('prologue_connects_to', []):
            refs.append({
                'type': 'prologue',
                'from_id': 'PROLOGUE',
                'from_title': '프롤로그',
                'trigger': '시작',
                'index': -1
            })

        return refs

    @staticmethod
    def delete_scene(scenario_data: Dict[str, Any], scene_id: str, handle_mode: str = 'remove_transitions') -> Tuple[Dict[str, Any], List[str]]:
        """
        씬 삭제 로직.
        handle_mode: 'remove_transitions' (연결된 선 삭제) | 'keep' (유지-깨진링크됨)
        Returns: (updated_scenario, warning_messages)
        """
        warnings = []
        updated_data = scenario_data.copy()

        # 씬 제거
        original_len = len(updated_data.get('scenes', []))
        updated_data['scenes'] = [s for s in updated_data.get('scenes', []) if s['scene_id'] != scene_id]

        if len(updated_data['scenes']) == original_len:
            return updated_data, ["삭제할 씬을 찾을 수 없습니다."]

        # 참조 정리
        if handle_mode == 'remove_transitions':
            count = 0
            for s in updated_data['scenes']:
                original_trans = s.get('transitions', [])
                # 타겟이 삭제된 씬인 전환 제거
                s['transitions'] = [t for t in original_trans if t.get('target_scene_id') != scene_id]
                if len(s['transitions']) != len(original_trans):
                    count += (len(original_trans) - len(s['transitions']))

            if count > 0:
                warnings.append(f"다른 씬에서 연결된 {count}개의 선택지가 함께 삭제되었습니다.")

            # 프롤로그 연결 정리
            if 'prologue_connects_to' in updated_data:
                updated_data['prologue_connects_to'] = [pid for pid in updated_data['prologue_connects_to'] if pid != scene_id]

        return updated_data, warnings

    @staticmethod
    def delete_ending(scenario_data: Dict[str, Any], ending_id: str) -> Tuple[Dict[str, Any], List[str]]:
        """엔딩 삭제"""
        warnings = []
        updated_data = scenario_data.copy()

        updated_data['endings'] = [e for e in updated_data.get('endings', []) if e['ending_id'] != ending_id]

        # 엔딩을 가리키던 전환 정리
        count = 0
        for s in updated_data.get('scenes', []):
            original_trans = s.get('transitions', [])
            s['transitions'] = [t for t in original_trans if t.get('target_scene_id') != ending_id]
            if len(s['transitions']) != len(original_trans):
                count += (len(original_trans) - len(s['transitions']))

        if count > 0:
            warnings.append(f"이 엔딩으로 향하는 {count}개의 선택지가 삭제되었습니다.")

        return updated_data, warnings

    @staticmethod
    def add_scene(scenario_data: Dict[str, Any], new_scene: Dict[str, Any], after_scene_id: Optional[str] = None) -> Dict[str, Any]:
        """새 씬 추가. ID 자동 생성."""
        updated_data = scenario_data.copy()
        scenes = updated_data.get('scenes', [])

        # ID 생성 (충돌 방지)
        base_id = "scene"
        idx = len(scenes) + 1
        existing_ids = {s['scene_id'] for s in scenes}
        while f"{base_id}_{idx}" in existing_ids:
            idx += 1

        final_id = new_scene.get('scene_id') or f"{base_id}_{idx}"
        new_scene['scene_id'] = final_id

        # 기본 필드 보장
        if 'transitions' not in new_scene: new_scene['transitions'] = []
        if 'title' not in new_scene: new_scene['title'] = '새로운 장면'

        # 삽입 위치
        if after_scene_id:
            insert_idx = -1
            for i, s in enumerate(scenes):
                if s['scene_id'] == after_scene_id:
                    insert_idx = i
                    break
            if insert_idx != -1:
                scenes.insert(insert_idx + 1, new_scene)
            else:
                scenes.append(new_scene)
        else:
            scenes.append(new_scene)

        updated_data['scenes'] = scenes
        return updated_data

    @staticmethod
    def add_ending(scenario_data: Dict[str, Any], new_ending: Dict[str, Any]) -> Dict[str, Any]:
        """새 엔딩 추가."""
        updated_data = scenario_data.copy()
        endings = updated_data.get('endings', [])

        idx = len(endings) + 1
        existing_ids = {e['ending_id'] for e in endings}
        while f"ending_{idx}" in existing_ids:
            idx += 1

        final_id = new_ending.get('ending_id') or f"ending_{idx}"
        new_ending['ending_id'] = final_id
        if 'title' not in new_ending: new_ending['title'] = '새 엔딩'

        endings.append(new_ending)
        updated_data['endings'] = endings
        return updated_data