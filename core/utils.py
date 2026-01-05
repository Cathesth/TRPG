"""
공통 유틸리티 함수
"""
import json
import logging
from typing import Dict, Any

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
      1) prologue_connects_to 중 실제 존재하는 씬
      2) 어떤 씬에서도 target으로 등장하지 않는 'root' 씬
      3) scenes[0]
      4) 'start'
    """
    if not isinstance(scenario, dict):
        return "start"

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

    for sid in scene_ids:
        if sid and sid not in targets and sid not in ('start', 'PROLOGUE'):
            return sid

    # 3) fallback
    first = scenes[0]
    if isinstance(first, dict) and first.get('scene_id'):
        return str(first.get('scene_id'))
    return "start"


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
