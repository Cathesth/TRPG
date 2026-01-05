"""
프리셋 관리 서비스
"""
import os
import json
import time
import logging
from typing import Dict, Any, List, Optional, Tuple

from config import PRESETS_FOLDER
from core.utils import sanitize_filename, ensure_directory

logger = logging.getLogger(__name__)


class PresetService:
    """프리셋 파일 관리 서비스"""

    @staticmethod
    def list_presets(sort_order: str = 'newest', user_id: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """프리셋 목록 조회"""
        ensure_directory(PRESETS_FOLDER)

        files = [f for f in os.listdir(PRESETS_FOLDER) if f.endswith('.json')]

        presets = []
        for f in files:
            file_path = os.path.join(PRESETS_FOLDER, f)
            try:
                with open(file_path, 'r', encoding='utf-8') as jf:
                    data = json.load(jf)
                    presets.append({
                        'filename': f,
                        'title': data.get('name', f.replace('.json', '')),
                        'desc': data.get('description', ''),
                        'author': data.get('author', 'Unknown'),
                        'is_owner': True,  # 파일 기반이므로 항상 소유자로 간주
                        'nodeCount': len(data.get('nodes', [])),
                        'npcCount': len(data.get('globalNpcs', [])),
                        'model': data.get('selectedModel', ''),
                        'createdAt': os.path.getctime(file_path)
                    })
            except Exception as e:
                logger.error(f"Error reading preset {f}: {e}")
                presets.append({
                    'filename': f,
                    'title': f.replace('.json', ''),
                    'desc': '',
                    'author': 'Unknown',
                    'is_owner': True,
                    'nodeCount': 0,
                    'npcCount': 0,
                    'model': '',
                    'createdAt': 0
                })

        # 정렬
        if sort_order == 'oldest':
            presets.sort(key=lambda x: x['createdAt'])
        else:  # newest
            presets.sort(key=lambda x: x['createdAt'], reverse=True)

        # 제한
        if limit:
            presets = presets[:limit]

        return presets

    @staticmethod
    def save_preset(data: Dict[str, Any], user_id: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
        """
        프리셋 저장

        Returns:
            (saved_filename, error_message)
        """
        name = data.get('name', '').strip()
        if not name:
            return None, "프리셋 이름을 입력하세요"

        safe_name = sanitize_filename(name, 'preset')

        preset_data = {
            'name': name,
            'description': data.get('description', ''),
            'author': user_id or 'Anonymous',
            'nodes': data.get('nodes', []),
            'connections': data.get('connections', []),
            'globalNpcs': data.get('globalNpcs', []),
            'selectedProvider': data.get('selectedProvider', 'deepseek'),
            'selectedModel': data.get('selectedModel', 'openai/tngtech/deepseek-r1t2-chimera:free'),
            'useAutoTitle': data.get('useAutoTitle', True)
        }

        ensure_directory(PRESETS_FOLDER)
        file_path = os.path.join(PRESETS_FOLDER, f"{safe_name}.json")

        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(preset_data, f, ensure_ascii=False, indent=2)
            return f"{safe_name}.json", None
        except Exception as e:
            logger.error(f"Preset Save Error: {e}")
            return None, str(e)

    @staticmethod
    def load_preset(filename: str, user_id: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        프리셋 로드

        Returns:
            ({'preset': preset_data}, error_message)
        """
        if not filename:
            return None, "파일명이 없습니다"

        # 보안: 경로 조작 방지
        if '..' in filename or '/' in filename or '\\' in filename:
            return None, "잘못된 파일명입니다"

        file_path = os.path.join(PRESETS_FOLDER, filename)

        if not os.path.exists(file_path):
            return None, "파일을 찾을 수 없습니다"

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                preset_data = json.load(f)
            return {'preset': preset_data}, None
        except Exception as e:
            logger.error(f"Preset Load Error: {e}")
            return None, str(e)

    @staticmethod
    def delete_preset(filename: str, user_id: Optional[str] = None) -> Tuple[bool, Optional[str]]:
        """
        프리셋 삭제

        Returns:
            (success, error_message)
        """
        if not filename:
            return False, "파일명이 없습니다"

        # 보안: 경로 조작 방지
        if '..' in filename or '/' in filename or '\\' in filename:
            return False, "잘못된 파일명입니다"

        file_path = os.path.join(PRESETS_FOLDER, filename)

        if not os.path.exists(file_path):
            return False, "파일을 찾을 수 없습니다"

        try:
            os.remove(file_path)
            return True, None
        except Exception as e:
            logger.error(f"Preset Delete Error: {e}")
            return False, str(e)
