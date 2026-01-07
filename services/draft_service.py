"""
Draft 시스템 서비스
- 임시 시나리오 저장/로드/최종 반영
- ID 순차 재정렬 및 참조 동기화
- 삭제 안전장치
- 특수문자 이스케이프 처리
"""
import re
import logging
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime
from collections import deque

from models import db, Scenario, TempScenario
from config import DEFAULT_PLAYER_VARS

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
        result = text
        for char, escape in DraftService.ESCAPE_MAP.items():
            result = result.replace(char, escape)
        return result

    @staticmethod
    def unescape_from_mermaid(text: str) -> str:
        """이스케이프된 문자열을 원본으로 복원"""
        if not text:
            return ''
        result = text
        for char, escape in DraftService.ESCAPE_MAP.items():
            result = result.replace(escape, char)
        return result

    @staticmethod
    def sanitize_scenario_data(scenario_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        시나리오 데이터의 모든 텍스트 필드를 이스케이프 처리
        Mermaid 렌더링 시 문법 파손 방지
        """
        sanitized = scenario_data.copy()

        # 프롤로그 이스케이프
        if 'prologue' in sanitized:
            sanitized['prologue'] = DraftService.escape_for_mermaid(sanitized['prologue'])
        if 'prologue_text' in sanitized:
            sanitized['prologue_text'] = DraftService.escape_for_mermaid(sanitized['prologue_text'])

        # 씬 이스케이프
        if 'scenes' in sanitized:
            for scene in sanitized['scenes']:
                if 'title' in scene:
                    scene['title'] = DraftService.escape_for_mermaid(scene['title'])
                if 'name' in scene:
                    scene['name'] = DraftService.escape_for_mermaid(scene['name'])
                # description은 Mermaid에 직접 표시되지 않으므로 이스케이프 불필요
                if 'transitions' in scene:
                    for trans in scene['transitions']:
                        if 'trigger' in trans:
                            trans['trigger'] = DraftService.escape_for_mermaid(trans['trigger'])
                        if 'condition' in trans:
                            trans['condition'] = DraftService.escape_for_mermaid(trans['condition'])

        # 엔딩 이스케이프
        if 'endings' in sanitized:
            for ending in sanitized['endings']:
                if 'title' in ending:
                    ending['title'] = DraftService.escape_for_mermaid(ending['title'])

        return sanitized

    @staticmethod
    def create_or_update_draft(scenario_id: int, user_id: str, data: Dict[str, Any] = None) -> Tuple[Optional[TempScenario], Optional[str]]:
        """
        Draft 생성 또는 업데이트
        이미 존재하면 업데이트, 없으면 새로 생성
        """
        try:
            # 원본 시나리오 확인
            scenario = Scenario.query.get(scenario_id)
            if not scenario:
                return None, "시나리오를 찾을 수 없습니다."

            if scenario.author_id != user_id:
                return None, "수정 권한이 없습니다."

            # 기존 Draft 확인
            draft = TempScenario.query.filter_by(
                original_scenario_id=scenario_id,
                editor_id=user_id
            ).first()

            if draft:
                # 기존 Draft 업데이트
                if data:
                    draft.data = data
                draft.updated_at = datetime.utcnow()
            else:
                # 새 Draft 생성 (원본 데이터 복사)
                original_data = scenario.data.get('scenario', scenario.data)
                draft = TempScenario(
                    original_scenario_id=scenario_id,
                    editor_id=user_id,
                    data=data if data else original_data
                )
                db.session.add(draft)

            db.session.commit()
            return draft, None

        except Exception as e:
            db.session.rollback()
            logger.error(f"Draft create/update error: {e}", exc_info=True)
            return None, str(e)

    @staticmethod
    def get_draft(scenario_id: int, user_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        Draft 로드 (없으면 원본 시나리오 데이터 반환)
        """
        try:
            # 원본 시나리오 확인
            scenario = Scenario.query.get(scenario_id)
            if not scenario:
                return None, "시나리오를 찾을 수 없습니다."

            if scenario.author_id != user_id:
                return None, "수정 권한이 없습니다."

            # Draft 확인
            draft = TempScenario.query.filter_by(
                original_scenario_id=scenario_id,
                editor_id=user_id
            ).first()

            if draft:
                return {
                    'draft_id': draft.id,
                    'scenario_id': scenario_id,
                    'scenario': draft.data,
                    'is_draft': True,
                    'updated_at': draft.updated_at.timestamp() if draft.updated_at else None
                }, None
            else:
                # Draft가 없으면 원본 반환
                original_data = scenario.data.get('scenario', scenario.data)
                return {
                    'draft_id': None,
                    'scenario_id': scenario_id,
                    'scenario': original_data,
                    'is_draft': False,
                    'updated_at': scenario.updated_at.timestamp() if scenario.updated_at else None
                }, None

        except Exception as e:
            logger.error(f"Draft get error: {e}", exc_info=True)
            return None, str(e)

    @staticmethod
    def save_draft(scenario_id: int, user_id: str, data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        Draft 저장 (자동 저장용)
        """
        draft, error = DraftService.create_or_update_draft(scenario_id, user_id, data)
        if error:
            return False, error
        return True, None

    @staticmethod
    def publish_draft(scenario_id: int, user_id: str) -> Tuple[bool, Optional[str]]:
        """
        Draft를 실제 시나리오로 최종 반영 (Publish)
        """
        try:
            # 원본 시나리오 확인
            scenario = Scenario.query.get(scenario_id)
            if not scenario:
                return False, "시나리오를 찾을 수 없습니다."

            if scenario.author_id != user_id:
                return False, "수정 권한이 없습니다."

            # Draft 확인
            draft = TempScenario.query.filter_by(
                original_scenario_id=scenario_id,
                editor_id=user_id
            ).first()

            if not draft:
                return False, "저장된 Draft가 없습니다."

            # Draft 데이터를 원본에 반영
            draft_scenario = draft.data

            # title 업데이트
            if 'title' in draft_scenario:
                scenario.title = draft_scenario['title']

            # 전체 데이터 업데이트
            current_data = scenario.data
            scenario.data = {
                "scenario": draft_scenario,
                "player_vars": current_data.get('player_vars', DEFAULT_PLAYER_VARS.copy())
            }
            scenario.updated_at = datetime.utcnow()

            # Draft 삭제
            db.session.delete(draft)
            db.session.commit()

            return True, None

        except Exception as e:
            db.session.rollback()
            logger.error(f"Publish draft error: {e}", exc_info=True)
            return False, str(e)

    @staticmethod
    def discard_draft(scenario_id: int, user_id: str) -> Tuple[bool, Optional[str]]:
        """
        Draft 폐기 (변경사항 취소)
        """
        try:
            draft = TempScenario.query.filter_by(
                original_scenario_id=scenario_id,
                editor_id=user_id
            ).first()

            if draft:
                db.session.delete(draft)
                db.session.commit()

            return True, None

        except Exception as e:
            db.session.rollback()
            logger.error(f"Discard draft error: {e}", exc_info=True)
            return False, str(e)

    @staticmethod
    def reorder_scene_ids(scenario_data: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
        """
        Mermaid 차트 흐름(위->아래, 좌->우)에 따라 씬 ID를 순차적으로 재정렬
        BFS 알고리즘 사용

        Returns:
            (재정렬된 시나리오, ID 매핑 {old_id: new_id})
        """
        scenes = scenario_data.get('scenes', [])
        endings = scenario_data.get('endings', [])
        prologue_connects_to = scenario_data.get('prologue_connects_to', [])

        if not scenes:
            return scenario_data, {}

        # 씬 ID -> 씬 객체 매핑
        scene_map = {s.get('scene_id'): s for s in scenes}

        # 시작점 찾기 (프롤로그에서 연결되는 씬 또는 다른 씬에서 참조되지 않는 씬)
        if not prologue_connects_to:
            all_target_ids = set()
            for scene in scenes:
                for trans in scene.get('transitions', []):
                    target_id = trans.get('target_scene_id')
                    if target_id and target_id in scene_map:
                        all_target_ids.add(target_id)

            root_scenes = [s.get('scene_id') for s in scenes if s.get('scene_id') not in all_target_ids]
            if not root_scenes:
                root_scenes = [scenes[0].get('scene_id')]
            prologue_connects_to = root_scenes

        # BFS로 순서 결정
        visited = set()
        order = []
        queue = deque(prologue_connects_to)

        while queue:
            current_id = queue.popleft()
            if current_id in visited or current_id not in scene_map:
                continue

            visited.add(current_id)
            order.append(current_id)

            # 현재 씬의 전환 대상들을 큐에 추가
            scene = scene_map[current_id]
            for trans in scene.get('transitions', []):
                target_id = trans.get('target_scene_id')
                if target_id and target_id in scene_map and target_id not in visited:
                    queue.append(target_id)

        # 방문하지 않은 씬들도 추가 (고립된 씬)
        for scene in scenes:
            scene_id = scene.get('scene_id')
            if scene_id not in visited:
                order.append(scene_id)

        # ID 매핑 생성 (old_id -> new_id)
        id_mapping = {}
        for idx, old_id in enumerate(order):
            new_id = f"scene_{idx + 1}"
            if old_id != new_id:
                id_mapping[old_id] = new_id

        # 매핑이 없으면 변경 불필요
        if not id_mapping:
            return scenario_data, {}

        # 시나리오 데이터 복사 및 ID 업데이트
        new_scenario = scenario_data.copy()
        new_scenes = []

        for scene in scenes:
            new_scene = scene.copy()
            old_id = scene.get('scene_id')

            # 씬 ID 변경
            if old_id in id_mapping:
                new_scene['scene_id'] = id_mapping[old_id]

            # 전환 대상 ID 변경
            if 'transitions' in new_scene:
                new_transitions = []
                for trans in new_scene['transitions']:
                    new_trans = trans.copy()
                    target_id = trans.get('target_scene_id')
                    if target_id in id_mapping:
                        new_trans['target_scene_id'] = id_mapping[target_id]
                    new_transitions.append(new_trans)
                new_scene['transitions'] = new_transitions

            new_scenes.append(new_scene)

        new_scenario['scenes'] = new_scenes

        # prologue_connects_to 업데이트
        if 'prologue_connects_to' in new_scenario:
            new_connects = [id_mapping.get(pid, pid) for pid in new_scenario['prologue_connects_to']]
            new_scenario['prologue_connects_to'] = new_connects

        return new_scenario, id_mapping

    @staticmethod
    def update_all_references(scenario_data: Dict[str, Any], id_mapping: Dict[str, str]) -> Dict[str, Any]:
        """
        ID 변경 시 시나리오 전체를 순회하여 해당 ID를 참조하는 모든 target_scene_id를 갱신
        """
        if not id_mapping:
            return scenario_data

        new_scenario = scenario_data.copy()

        # 씬 내 전환 대상 업데이트
        if 'scenes' in new_scenario:
            for scene in new_scenario['scenes']:
                if 'transitions' in scene:
                    for trans in scene['transitions']:
                        target_id = trans.get('target_scene_id')
                        if target_id in id_mapping:
                            trans['target_scene_id'] = id_mapping[target_id]

        # prologue_connects_to 업데이트
        if 'prologue_connects_to' in new_scenario:
            new_scenario['prologue_connects_to'] = [
                id_mapping.get(pid, pid) for pid in new_scenario['prologue_connects_to']
            ]

        return new_scenario

    @staticmethod
    def check_scene_references(scenario_data: Dict[str, Any], scene_id: str) -> List[Dict[str, Any]]:
        """
        특정 씬을 참조하는 모든 씬/전환 정보 반환
        삭제 안전장치용
        """
        references = []
        scenes = scenario_data.get('scenes', [])
        prologue_connects_to = scenario_data.get('prologue_connects_to', [])

        # 프롤로그에서 참조
        if scene_id in prologue_connects_to:
            references.append({
                'from_scene': 'PROLOGUE',
                'from_title': '프롤로그',
                'transition_index': prologue_connects_to.index(scene_id),
                'type': 'prologue'
            })

        # 다른 씬에서 참조
        for scene in scenes:
            if scene.get('scene_id') == scene_id:
                continue

            for idx, trans in enumerate(scene.get('transitions', [])):
                if trans.get('target_scene_id') == scene_id:
                    references.append({
                        'from_scene': scene.get('scene_id'),
                        'from_title': scene.get('title') or scene.get('name') or scene.get('scene_id'),
                        'transition_index': idx,
                        'trigger': trans.get('trigger') or trans.get('condition') or '자유 행동',
                        'type': 'scene'
                    })

        return references

    @staticmethod
    def delete_scene(scenario_data: Dict[str, Any], scene_id: str,
                     handle_mode: str = 'remove_transitions') -> Tuple[Dict[str, Any], List[str]]:
        """
        씬 삭제 및 연결 처리

        handle_mode:
            - 'remove_transitions': 해당 씬으로 가는 전환을 모두 삭제
            - 'redirect_to': 다른 씬으로 재연결 (redirect_target 필요)

        Returns:
            (수정된 시나리오, 경고 메시지 목록)
        """
        new_scenario = scenario_data.copy()
        warnings = []

        scenes = new_scenario.get('scenes', [])

        # 해당 씬 삭제
        new_scenes = [s for s in scenes if s.get('scene_id') != scene_id]
        if len(new_scenes) == len(scenes):
            return new_scenario, ["삭제할 씬을 찾을 수 없습니다."]

        new_scenario['scenes'] = new_scenes

        # 프롤로그 연결 처리
        if 'prologue_connects_to' in new_scenario:
            new_connects = [pid for pid in new_scenario['prologue_connects_to'] if pid != scene_id]
            if len(new_connects) != len(new_scenario['prologue_connects_to']):
                warnings.append(f"프롤로그에서 '{scene_id}'로의 연결이 제거되었습니다.")
            new_scenario['prologue_connects_to'] = new_connects

        # 다른 씬의 전환 처리
        if handle_mode == 'remove_transitions':
            for scene in new_scenario['scenes']:
                if 'transitions' in scene:
                    original_count = len(scene['transitions'])
                    scene['transitions'] = [
                        t for t in scene['transitions']
                        if t.get('target_scene_id') != scene_id
                    ]
                    removed = original_count - len(scene['transitions'])
                    if removed > 0:
                        warnings.append(f"'{scene.get('title', scene.get('scene_id'))}'에서 '{scene_id}'로의 전환 {removed}개가 제거되었습니다.")

        return new_scenario, warnings

    @staticmethod
    def add_scene(scenario_data: Dict[str, Any], new_scene: Dict[str, Any],
                  after_scene_id: str = None) -> Dict[str, Any]:
        """
        새 씬 추가

        after_scene_id: 이 씬 다음에 추가 (없으면 마지막에 추가)
        """
        new_scenario = scenario_data.copy()
        scenes = new_scenario.get('scenes', [])

        # scene_id 자동 생성
        if not new_scene.get('scene_id'):
            existing_ids = {s.get('scene_id') for s in scenes}
            idx = len(scenes) + 1
            while f"scene_{idx}" in existing_ids:
                idx += 1
            new_scene['scene_id'] = f"scene_{idx}"

        # 기본 필드 설정
        if 'transitions' not in new_scene:
            new_scene['transitions'] = []

        # 삽입 위치 결정
        if after_scene_id:
            insert_idx = None
            for idx, scene in enumerate(scenes):
                if scene.get('scene_id') == after_scene_id:
                    insert_idx = idx + 1
                    break
            if insert_idx is not None:
                scenes.insert(insert_idx, new_scene)
            else:
                scenes.append(new_scene)
        else:
            scenes.append(new_scene)

        new_scenario['scenes'] = scenes
        return new_scenario

    @staticmethod
    def add_ending(scenario_data: Dict[str, Any], new_ending: Dict[str, Any]) -> Dict[str, Any]:
        """새 엔딩 추가"""
        new_scenario = scenario_data.copy()
        endings = new_scenario.get('endings', [])

        # ending_id 자동 생성
        if not new_ending.get('ending_id'):
            existing_ids = {e.get('ending_id') for e in endings}
            idx = len(endings) + 1
            while f"ending_{idx}" in existing_ids:
                idx += 1
            new_ending['ending_id'] = f"ending_{idx}"

        endings.append(new_ending)
        new_scenario['endings'] = endings
        return new_scenario

    @staticmethod
    def delete_ending(scenario_data: Dict[str, Any], ending_id: str) -> Tuple[Dict[str, Any], List[str]]:
        """엔딩 삭제"""
        new_scenario = scenario_data.copy()
        warnings = []

        endings = new_scenario.get('endings', [])
        new_endings = [e for e in endings if e.get('ending_id') != ending_id]

        if len(new_endings) == len(endings):
            return new_scenario, ["삭제할 엔딩을 찾을 수 없습니다."]

        new_scenario['endings'] = new_endings

        # 해당 엔딩을 참조하는 전환 제거
        for scene in new_scenario.get('scenes', []):
            if 'transitions' in scene:
                original_count = len(scene['transitions'])
                scene['transitions'] = [
                    t for t in scene['transitions']
                    if t.get('target_scene_id') != ending_id
                ]
                removed = original_count - len(scene['transitions'])
                if removed > 0:
                    warnings.append(f"'{scene.get('title', scene.get('scene_id'))}'에서 '{ending_id}'로의 전환이 제거되었습니다.")

        return new_scenario, warnings

