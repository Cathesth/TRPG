"""
시나리오 로드/저장 서비스
"""
import os
import json
import time
import logging
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime

from config import DB_FOLDER, DEFAULT_PLAYER_VARS
from core.utils import sanitize_filename, ensure_directory

logger = logging.getLogger(__name__)


class ScenarioService:
    """시나리오 파일 관리 서비스"""

    @staticmethod
    def list_scenarios(sort_order: str = 'newest') -> List[Dict[str, Any]]:
        """
        시나리오 목록 조회

        Args:
            sort_order: 정렬 기준 (newest, oldest, name_asc, name_desc)

        Returns:
            시나리오 정보 리스트
        """
        ensure_directory(DB_FOLDER)

        files = [f for f in os.listdir(DB_FOLDER) if f.endswith('.json')]

        file_infos = []
        for f in files:
            file_path = os.path.join(DB_FOLDER, f)
            title = f.replace('.json', '')
            desc = "저장된 시나리오"

            try:
                created_time = os.path.getctime(file_path)
            except:
                created_time = 0

            try:
                with open(file_path, 'r', encoding='utf-8') as jf:
                    data = json.load(jf)
                    s_data = data.get('scenario', data)
                    title = s_data.get('title', title)
                    p_text = s_data.get('prologue', s_data.get('prologue_text', ''))
                    if p_text:
                        desc = p_text[:60] + "..."
            except:
                pass

            file_infos.append({
                'filename': f,
                'path': file_path,
                'created_time': created_time,
                'title': title,
                'desc': desc
            })

        # 정렬
        if sort_order == 'oldest':
            file_infos.sort(key=lambda x: x['created_time'])
        elif sort_order == 'name_asc':
            file_infos.sort(key=lambda x: x['title'].lower())
        elif sort_order == 'name_desc':
            file_infos.sort(key=lambda x: x['title'].lower(), reverse=True)
        else:  # newest
            file_infos.sort(key=lambda x: x['created_time'], reverse=True)

        return file_infos

    @staticmethod
    def load_scenario(filename: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        시나리오 파일 로드

        Returns:
            (scenario_data, error_message)
        """
        if not filename:
            return None, "파일명 누락"

        file_path = os.path.join(DB_FOLDER, filename)

        if not os.path.exists(file_path):
            return None, f"파일을 찾을 수 없습니다: {filename}"

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                full_data = json.load(f)

            scenario = full_data.get('scenario', full_data)
            initial_vars = full_data.get('player_vars', scenario.get('initial_state', {}))

            # global variables 초기값 병합
            raw_vars = scenario.get('variables', [])
            if isinstance(raw_vars, list):
                for g_var in raw_vars:
                    if isinstance(g_var, dict):
                        v_name = g_var.get('name')
                        if v_name and v_name not in initial_vars:
                            initial_vars[v_name] = g_var.get('initial_value', 0)
                    elif isinstance(g_var, str):
                        if g_var not in initial_vars:
                            initial_vars[g_var] = 0

            # 기본값 보장
            for key, value in DEFAULT_PLAYER_VARS.items():
                if key not in initial_vars:
                    initial_vars[key] = value

            return {
                'scenario': scenario,
                'player_vars': initial_vars
            }, None

        except Exception as e:
            logger.error(f"Load Error: {e}", exc_info=True)
            return None, str(e)

    @staticmethod
    def save_scenario(scenario_json: Dict[str, Any], player_vars: Dict[str, Any] = None) -> Tuple[Optional[str], Optional[str]]:
        """
        시나리오 저장

        Returns:
            (saved_filename, error_message)
        """
        try:
            title = scenario_json.get('title', 'Untitled_Scenario')
            safe_title = sanitize_filename(title, 'scenario')

            ensure_directory(DB_FOLDER)

            if player_vars is None:
                player_vars = {}
                variables = scenario_json.get('variables', [])
                if isinstance(variables, list):
                    for v in variables:
                        if isinstance(v, dict):
                            player_vars[v.get('name', 'unknown')] = v.get('initial_value', 0)

                for key, value in DEFAULT_PLAYER_VARS.items():
                    if key not in player_vars:
                        player_vars[key] = value

            save_path = os.path.join(DB_FOLDER, f"{safe_title}.json")

            with open(save_path, 'w', encoding='utf-8') as f:
                json.dump({
                    "scenario": scenario_json,
                    "player_vars": player_vars
                }, f, ensure_ascii=False, indent=2)

            return f"{safe_title}.json", None

        except Exception as e:
            logger.error(f"Save Error: {e}", exc_info=True)
            return None, str(e)

    @staticmethod
    def delete_scenario(filename: str) -> Tuple[bool, Optional[str]]:
        """
        시나리오 삭제

        Returns:
            (success, error_message)
        """
        if not filename:
            return False, "파일명이 없습니다."

        # 보안: 경로 조작 방지
        if '..' in filename or '/' in filename or '\\' in filename:
            return False, "잘못된 파일명입니다."

        file_path = os.path.join(DB_FOLDER, filename)

        if not os.path.exists(file_path):
            return False, "파일을 찾을 수 없습니다."

        try:
            os.remove(file_path)
            return True, None
        except Exception as e:
            return False, str(e)

    @staticmethod
    def is_recently_created(created_time: float, threshold_seconds: int = 600) -> bool:
        """생성된지 threshold_seconds 이내인지 확인 (기본 10분)"""
        return (time.time() - created_time) < threshold_seconds

    @staticmethod
    def format_time(timestamp: float) -> str:
        """타임스탬프를 포맷된 문자열로 변환"""
        if timestamp <= 0:
            return ""
        dt = datetime.fromtimestamp(timestamp)
        return dt.strftime('%Y-%m-%d %H:%M')

