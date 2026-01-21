"""
공통 유틸리티 함수
"""
import json
import logging
from typing import Dict, Any, List, Set, Tuple, Optional
from collections import deque

logger = logging.getLogger(__name__)


# ============================================================================
# 그래프 기반 시나리오 유효성 검사 (Validation Utils)
# ============================================================================

class ScenarioValidationResult:
    """유효성 검사 결과를 담는 클래스"""

    def __init__(self):
        self.is_valid: bool = True
        self.errors: List[Dict[str, Any]] = []
        self.warnings: List[Dict[str, Any]] = []
        self.isolated_nodes: List[str] = []
        self.broken_references: List[Dict[str, str]] = []
        self.unreachable_endings: List[str] = []
        self.reachable_endings: List[str] = []

    def add_error(self, error_type: str, message: str, node_id: str = None, details: Dict = None):
        """에러 추가"""
        self.is_valid = False
        self.errors.append({
            "type": error_type,
            "message": message,
            "node_id": node_id,
            "details": details or {}
        })

    def add_warning(self, warning_type: str, message: str, node_id: str = None, details: Dict = None):
        """경고 추가 (유효성에 영향 없음)"""
        self.warnings.append({
            "type": warning_type,
            "message": message,
            "node_id": node_id,
            "details": details or {}
        })

    def to_dict(self) -> Dict[str, Any]:
        """결과를 딕셔너리로 변환"""
        return {
            "is_valid": self.is_valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "isolated_nodes": self.isolated_nodes,
            "broken_references": self.broken_references,
            "unreachable_endings": self.unreachable_endings,
            "reachable_endings": self.reachable_endings,
            "summary": {
                "error_count": len(self.errors),
                "warning_count": len(self.warnings),
                "isolated_node_count": len(self.isolated_nodes),
                "broken_reference_count": len(self.broken_references),
                "reachable_ending_count": len(self.reachable_endings),
                "unreachable_ending_count": len(self.unreachable_endings)
            }
        }


def build_scene_graph(scenario: Dict[str, Any]) -> Tuple[Dict[str, List[str]], Set[str], Set[str], str]:
    """
    시나리오에서 그래프 구조를 추출

    Returns:
        adjacency: 인접 리스트 (scene_id -> [target_scene_ids])
        scene_ids: 모든 씬 ID 집합
        ending_ids: 모든 엔딩 ID 집합
        start_scene_id: 시작 씬 ID
    """
    scenes = scenario.get('scenes', [])
    endings = scenario.get('endings', [])

    scene_ids: Set[str] = set()
    ending_ids: Set[str] = set()
    adjacency: Dict[str, List[str]] = {}

    # 씬 ID 수집
    for scene in scenes:
        if isinstance(scene, dict):
            sid = scene.get('scene_id')
            if sid:
                scene_ids.add(sid)
                adjacency[sid] = []

    # 엔딩 ID 수집
    for ending in endings:
        if isinstance(ending, dict):
            eid = ending.get('ending_id')
            if eid:
                ending_ids.add(eid)
                adjacency[eid] = []  # 엔딩은 나가는 엣지가 없음

    # 프롤로그 처리
    prologue_text = scenario.get('prologue') or scenario.get('prologue_text', '')
    has_prologue = bool(prologue_text and prologue_text.strip())

    if has_prologue:
        scene_ids.add('prologue')
        adjacency['prologue'] = []

        # 프롤로그 연결 처리
        prologue_connects = scenario.get('prologue_connects_to', [])
        if isinstance(prologue_connects, list):
            for target in prologue_connects:
                if isinstance(target, str) and target:
                    adjacency['prologue'].append(target)

        # prologue_connects_to가 없으면 첫 번째 씬으로 연결
        if not adjacency['prologue'] and scenes:
            first_scene = scenes[0]
            if isinstance(first_scene, dict) and first_scene.get('scene_id'):
                adjacency['prologue'].append(first_scene['scene_id'])

    # 씬 간 전이(transitions) 연결
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        sid = scene.get('scene_id')
        if not sid:
            continue

        transitions = scene.get('transitions', [])
        if isinstance(transitions, list):
            for trans in transitions:
                if isinstance(trans, dict):
                    target = trans.get('target_scene_id')
                    if isinstance(target, str) and target:
                        adjacency[sid].append(target)

    # 시작 씬 결정
    start_scene_id = pick_start_scene_id(scenario)

    return adjacency, scene_ids, ending_ids, start_scene_id


def find_isolated_nodes(scenario: Dict[str, Any]) -> List[str]:
    """
    고립 노드 탐지: 시작 씬에서 BFS로 도달할 수 없는 씬들을 찾음

    Args:
        scenario: 시나리오 데이터

    Returns:
        고립된 노드 ID 목록
    """
    adjacency, scene_ids, ending_ids, start_scene_id = build_scene_graph(scenario)

    all_nodes = scene_ids | ending_ids
    if not all_nodes:
        return []

    # BFS로 시작 씬에서 도달 가능한 모든 노드 찾기
    reachable: Set[str] = set()
    queue = deque([start_scene_id])

    while queue:
        current = queue.popleft()
        if current in reachable:
            continue
        reachable.add(current)

        # 인접 노드 탐색
        for neighbor in adjacency.get(current, []):
            if neighbor not in reachable and neighbor in adjacency:
                queue.append(neighbor)

    # 도달 불가능한 노드 = 고립 노드
    isolated = [node for node in all_nodes if node not in reachable and node != start_scene_id]

    return isolated


def find_broken_references(scenario: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    참조 무결성 검사: 존재하지 않는 ID를 가리키는 target_scene_id 탐지

    Args:
        scenario: 시나리오 데이터

    Returns:
        깨진 참조 목록 [{"from_scene_id": ..., "target_scene_id": ..., "trigger": ...}, ...]
    """
    scenes = scenario.get('scenes', [])
    endings = scenario.get('endings', [])

    # 유효한 ID 집합 구성
    valid_ids: Set[str] = set()

    for scene in scenes:
        if isinstance(scene, dict) and scene.get('scene_id'):
            valid_ids.add(scene['scene_id'])

    for ending in endings:
        if isinstance(ending, dict) and ending.get('ending_id'):
            valid_ids.add(ending['ending_id'])

    broken_refs: List[Dict[str, str]] = []

    # 프롤로그 연결 검사
    prologue_connects = scenario.get('prologue_connects_to', [])
    if isinstance(prologue_connects, list):
        for target in prologue_connects:
            if isinstance(target, str) and target and target not in valid_ids:
                broken_refs.append({
                    "from_scene_id": "prologue",
                    "target_scene_id": target,
                    "trigger": "prologue_connects_to"
                })

    # 씬 전이 검사
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        sid = scene.get('scene_id')
        if not sid:
            continue

        transitions = scene.get('transitions', [])
        if isinstance(transitions, list):
            for trans in transitions:
                if isinstance(trans, dict):
                    target = trans.get('target_scene_id')
                    trigger = trans.get('trigger', trans.get('condition', ''))

                    if isinstance(target, str) and target and target not in valid_ids:
                        broken_refs.append({
                            "from_scene_id": sid,
                            "target_scene_id": target,
                            "trigger": trigger or "(조건 없음)"
                        })

    return broken_refs


def check_ending_reachability(scenario: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """
    도달 가능성 검사: 시작 씬에서 각 엔딩까지 도달 가능한 경로 존재 여부 확인

    Args:
        scenario: 시나리오 데이터

    Returns:
        (reachable_endings, unreachable_endings) 튜플
    """
    adjacency, scene_ids, ending_ids, start_scene_id = build_scene_graph(scenario)

    if not ending_ids:
        return [], []

    # BFS로 시작 씬에서 도달 가능한 모든 노드 찾기
    reachable: Set[str] = set()
    queue = deque([start_scene_id])

    while queue:
        current = queue.popleft()
        if current in reachable:
            continue
        reachable.add(current)

        for neighbor in adjacency.get(current, []):
            if neighbor not in reachable:
                # 존재하는 노드인지 확인 (참조 무결성과 별개로 처리)
                if neighbor in adjacency or neighbor in ending_ids:
                    queue.append(neighbor)

    reachable_endings = [eid for eid in ending_ids if eid in reachable]
    unreachable_endings = [eid for eid in ending_ids if eid not in reachable]

    return reachable_endings, unreachable_endings


def find_path_to_ending(scenario: Dict[str, Any], target_ending_id: str) -> Optional[List[str]]:
    """
    DFS로 시작 씬에서 특정 엔딩까지의 경로 찾기

    Args:
        scenario: 시나리오 데이터
        target_ending_id: 목표 엔딩 ID

    Returns:
        경로 리스트 또는 None (도달 불가)
    """
    adjacency, scene_ids, ending_ids, start_scene_id = build_scene_graph(scenario)

    if target_ending_id not in ending_ids:
        return None

    # DFS로 경로 찾기
    visited: Set[str] = set()
    path: List[str] = []

    def dfs(node: str) -> bool:
        if node in visited:
            return False

        visited.add(node)
        path.append(node)

        if node == target_ending_id:
            return True

        for neighbor in adjacency.get(node, []):
            if dfs(neighbor):
                return True

        path.pop()
        return False

    if dfs(start_scene_id):
        return path
    return None


def validate_scenario_graph(scenario: Dict[str, Any]) -> ScenarioValidationResult:
    """
    시나리오 그래프 전체 유효성 검사

    검사 항목:
    1. 고립 노드: 어떤 경로로도 도달할 수 없는 씬 적발
    2. 참조 무결성: 존재하지 않는 ID를 가리키는 target_scene_id 적발
    3. 도달 가능성: 시작 씬에서 하나 이상의 엔딩까지 도달 가능한 경로 존재 여부

    Args:
        scenario: 시나리오 데이터

    Returns:
        ScenarioValidationResult 객체
    """
    result = ScenarioValidationResult()

    # 기본 구조 검사
    scenes = scenario.get('scenes', [])
    endings = scenario.get('endings', [])

    if not scenes:
        result.add_error(
            "NO_SCENES",
            "시나리오에 씬이 없습니다.",
            details={"required": "최소 1개 이상의 씬이 필요합니다."}
        )
        return result

    if not endings:
        result.add_warning(
            "NO_ENDINGS",
            "시나리오에 엔딩이 없습니다.",
            details={"suggestion": "최소 1개 이상의 엔딩을 추가하는 것을 권장합니다."}
        )

    # 1. 고립 노드 검사
    isolated_nodes = find_isolated_nodes(scenario)
    result.isolated_nodes = isolated_nodes

    for node_id in isolated_nodes:
        result.add_error(
            "ISOLATED_NODE",
            f"고립된 노드: '{node_id}'에 도달할 수 있는 경로가 없습니다.",
            node_id=node_id,
            details={"suggestion": "다른 씬에서 이 노드로 연결하는 전이(transition)를 추가하세요."}
        )

    # 2. 참조 무결성 검사
    broken_refs = find_broken_references(scenario)
    result.broken_references = broken_refs

    for ref in broken_refs:
        result.add_error(
            "BROKEN_REFERENCE",
            f"깨진 참조: '{ref['from_scene_id']}'에서 존재하지 않는 '{ref['target_scene_id']}'를 참조합니다.",
            node_id=ref['from_scene_id'],
            details={
                "target": ref['target_scene_id'],
                "trigger": ref['trigger'],
                "suggestion": "올바른 씬/엔딩 ID로 수정하거나 해당 씬/엔딩을 생성하세요."
            }
        )

    # 3. 도달 가능성 검사
    reachable_endings, unreachable_endings = check_ending_reachability(scenario)
    result.reachable_endings = reachable_endings
    result.unreachable_endings = unreachable_endings

    if endings and not reachable_endings:
        result.add_error(
            "NO_REACHABLE_ENDING",
            "도달 가능한 엔딩이 없습니다. 시작 씬에서 엔딩까지의 경로가 존재하지 않습니다.",
            details={"suggestion": "씬 간 전이(transition)를 확인하고 엔딩으로 연결되는 경로를 만드세요."}
        )

    for eid in unreachable_endings:
        result.add_warning(
            "UNREACHABLE_ENDING",
            f"도달 불가 엔딩: '{eid}'에 도달할 수 있는 경로가 없습니다.",
            node_id=eid,
            details={"suggestion": "이 엔딩으로 연결되는 전이(transition)를 추가하세요."}
        )

    return result


def can_publish_scenario(scenario: Dict[str, Any]) -> Tuple[bool, ScenarioValidationResult]:
    """
    시나리오 최종 반영(publish) 가능 여부 확인
    유효성 검사 통과 전까지는 최종 반영을 차단

    Args:
        scenario: 시나리오 데이터

    Returns:
        (can_publish: bool, validation_result: ScenarioValidationResult)
    """
    result = validate_scenario_graph(scenario)
    return result.is_valid, result


# ============================================================================
# 기존 유틸리티 함수들
# ============================================================================

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
      1) prologue가 있으면 무조건 'prologue' 반환
      2) start_scene_id가 명시적으로 지정된 경우
      3) prologue_connects_to 중 실제 존재하는 씬
      4) 어떤 씬에서도 target으로 등장하지 않는 'root' 씬
      5) scenes[0]
      6) 'start'
    """
    if not isinstance(scenario, dict):
        return "start"

    # 1) 프롤로그가 있으면 무조건 프롤로그부터 시작
    prologue_text = scenario.get('prologue') or scenario.get('prologue_text', '')
    if prologue_text and prologue_text.strip():
        return 'prologue'

    # 2) 명시적으로 start_scene_id가 지정된 경우
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

    # 3) prologue_connects_to 우선
    connects = scenario.get('prologue_connects_to', [])
    if isinstance(connects, list):
        for sid in connects:
            if isinstance(sid, str) and sid in valid_ids:
                return sid

    # 4) root scene 자동 탐지 (target으로 한 번도 등장하지 않는 씬)
    targets = set()
    for s in scenes:
        if not isinstance(s, dict):
            continue
        for trans in s.get('transitions', []) or []:
            if isinstance(trans, dict):
                tid = trans.get('target_scene_id')
                if isinstance(tid, str) and tid:
                    targets.add(tid)

    for sid in scene_ids:
        if sid and sid not in targets and sid not in ('start', 'PROLOGUE'):
            return str(sid)

    # 5) fallback
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
