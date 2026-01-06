"""
공통 유틸리티 함수
"""
import json
import logging
from typing import Dict, Any, List
from collections import deque

logger = logging.getLogger(__name__)


def parse_request_data(req) -> Dict[str, Any]:
    """
    Flask request에서 JSON 데이터를 안전하게 파싱
    이중 인코딩이나 Content-Type 헤더 문제를 처리
    """
    try:
        # 1. 기본 json 파싱 시도 (force=True로 헤더 무시하고 시도)
        data = req.get_json(force=True, silent=True)

        # 2. 만약 data가 None이거나(파싱실패) 문자열이면(이중인코딩) 추가 처리
        if data is None:
            data = req.data.decode('utf-8')

        if isinstance(data, str):
            if not data.strip():
                return {}
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                logger.warning(f"JSON 파싱 실패, 원본 데이터: {data[:100]}...")
                return {}

        return data if isinstance(data, dict) else {}

    except Exception as e:
        logger.error(f"데이터 파싱 중 치명적 오류: {e}")
        return {}


def pick_start_scene_id(scenario: dict) -> str:
    """
    시나리오 시작 씬을 결정
    우선순위:
      1) start_scene_id가 명시적으로 지정된 경우
      2) prologue_connects_to 중 실제 존재하는 씬
      3) 어떤 씬에서도 target으로 등장하지 않는 'root' 씬
      4) scenes[0]
      5) 'start'
    """
    if not isinstance(scenario, dict):
        return "start"

    # 명시적으로 start_scene_id가 지정된 경우 우선
    explicit_start = scenario.get('start_scene_id')
    if explicit_start and isinstance(explicit_start, str):
        scenes = scenario.get('scenes', [])
        scene_ids = {s.get('scene_id') for s in scenes if isinstance(s, dict) and s.get('scene_id')}
        if explicit_start in scene_ids:
            return explicit_start

    scenes = scenario.get('scenes', [])
    if not isinstance(scenes, list) or not scenes:
        return "start"

    scene_ids = [s.get('scene_id') for s in scenes if isinstance(s, dict) and s.get('scene_id')]
    valid_ids = set(scene_ids)

    # 1) prologue_connects_to 우선
    connects = scenario.get('prologue_connects_to', [])
    if isinstance(connects, list):
        for sid in connects:
            if isinstance(sid, str) and sid in valid_ids:
                return sid

    # 2) root scene 자동 탐지 (target으로 한 번도 등장하지 않는 씬)
    targets = set()
    for s in scenes:
        if not isinstance(s, dict):
            continue
        for trans in s.get('transitions', []) or []:
            if isinstance(trans, dict):
                tid = trans.get('target_scene_id')
                if isinstance(tid, str) and tid:
                    targets.add(tid)

    # Scene-1 패턴의 씬이 있으면 우선 선택
    for sid in scene_ids:
        if sid and sid.lower() in ('scene-1', 'scene_1', 'scene1'):
            return str(sid)

    for sid in scene_ids:
        if sid and sid not in targets and sid not in ('start', 'PROLOGUE'):
            return str(sid)

    # 3) fallback
    first = scenes[0]
    if isinstance(first, dict) and first.get('scene_id'):
        return str(first.get('scene_id'))
    return "start"


def renumber_scenes_bfs(scenario: dict) -> dict:
    """
    BFS 순서대로 씬에 번호를 다시 매김 (위에서 아래, 왼쪽에서 오른쪽)
    프롤로그 바로 아래가 Scene-1이 되도록 함
    """
    if not isinstance(scenario, dict):
        return scenario

    scenes = scenario.get('scenes', [])
    if not scenes:
        return scenario

    # 시작점 찾기
    start_id = pick_start_scene_id(scenario)

    # 씬 ID -> 씬 객체 매핑
    scene_map = {s['scene_id']: s for s in scenes if isinstance(s, dict) and s.get('scene_id')}

    # 인접 리스트 생성
    adjacency = {}
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        sid = scene.get('scene_id')
        transitions = scene.get('transitions', []) or []
        adjacency[sid] = [t.get('target_scene_id') for t in transitions if isinstance(t, dict) and t.get('target_scene_id')]

    # BFS로 순회하며 순서 결정
    visited = set()
    order = []
    queue = deque([start_id])

    while queue:
        current = queue.popleft()
        if current in visited or current not in scene_map:
            continue
        visited.add(current)
        order.append(current)

        # 자식 노드들을 큐에 추가
        for next_id in adjacency.get(current, []):
            if next_id not in visited and next_id in scene_map:
                queue.append(next_id)

    # 방문하지 않은 씬도 추가
    for sid in scene_map:
        if sid not in visited:
            order.append(sid)

    # 새로운 ID 매핑 생성
    id_mapping = {}
    for idx, old_id in enumerate(order):
        new_id = f"Scene-{idx + 1}"
        id_mapping[old_id] = new_id

    # 씬 업데이트
    new_scenes = []
    for old_id in order:
        scene = scene_map[old_id].copy()
        old_scene_id = scene['scene_id']
        scene['scene_id'] = id_mapping.get(old_scene_id, old_scene_id)

        # 트랜지션의 target_scene_id도 업데이트
        if scene.get('transitions'):
            new_transitions = []
            for trans in scene['transitions']:
                new_trans = trans.copy()
                old_target = trans.get('target_scene_id')
                if old_target in id_mapping:
                    new_trans['target_scene_id'] = id_mapping[old_target]
                new_transitions.append(new_trans)
            scene['transitions'] = new_transitions

        new_scenes.append(scene)

    # 엔딩의 incoming transition도 업데이트
    endings = scenario.get('endings', [])
    # 엔딩 ID는 유지 (엔딩은 씬이 아님)

    scenario['scenes'] = new_scenes
    if start_id in id_mapping:
        scenario['start_scene_id'] = id_mapping[start_id]

    # prologue_connects_to 업데이트
    connects = scenario.get('prologue_connects_to', [])
    if connects:
        scenario['prologue_connects_to'] = [id_mapping.get(c, c) for c in connects]

    return scenario


def sanitize_filename(name: str, default_prefix: str = "file") -> str:
    """
    안전한 파일명 생성
    """
    import time
    safe_name = "".join([
        c for c in name
        if c.isalnum() or c in (' ', '-', '_') or '\uac00' <= c <= '\ud7a3'
    ]).strip().replace(' ', '_')

    if not safe_name:
        safe_name = f"{default_prefix}_{int(time.time())}"

    return safe_name


def ensure_directory(path: str):
    """디렉토리가 없으면 생성"""
    import os
    if not os.path.exists(path):
        os.makedirs(path)
