"""
프리셋 관리 서비스 (PostgreSQL DB 기반)
"""
import json
import logging
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime

from models import db, Preset

logger = logging.getLogger(__name__)


class PresetService:
    """프리셋 DB 관리 서비스"""

    @staticmethod
    def list_presets(sort_order: str = 'newest', user_id: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """프리셋 목록 조회 (DB 기반)"""
        query = Preset.query

        # 필터링: 모든 프리셋 표시 (공개/비공개 구분 없음, 필요시 추가 가능)
        # 현재는 로그인한 사용자가 자신의 프리셋을 볼 수 있도록만 구현
        if user_id:
            query = query.filter_by(author_id=user_id)

        # 정렬
        if sort_order == 'oldest':
            query = query.order_by(Preset.created_at.asc())
        elif sort_order == 'name_asc':
            query = query.order_by(Preset.name.asc())
        elif sort_order == 'name_desc':
            query = query.order_by(Preset.name.desc())
        else:  # newest
            query = query.order_by(Preset.created_at.desc())

        # 제한
        if limit:
            query = query.limit(limit)

        presets = query.all()
        preset_infos = []

        for p in presets:
            p_data = p.data or {}

            preset_infos.append({
                'filename': str(p.id),  # DB ID를 filename처럼 사용 (호환성)
                'id': p.id,
                'title': p.name,
                'desc': p.description or '',
                'author': p.author_id or 'Anonymous',
                'is_owner': (user_id is not None) and (p.author_id == user_id),
                'nodeCount': len(p_data.get('nodes', [])),
                'npcCount': len(p_data.get('globalNpcs', [])),
                'model': p_data.get('selectedModel', ''),
                'createdAt': p.created_at.timestamp()
            })

        return preset_infos

    @staticmethod
    def save_preset(data: Dict[str, Any], user_id: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
        """
        프리셋 저장 (DB)

        Returns:
            (saved_id, error_message)
        """
        name = data.get('name', '').strip()
        if not name:
            return None, "프리셋 이름을 입력하세요"

        description = data.get('description', '')

        # 프리셋 데이터 구성
        preset_data = {
            'nodes': data.get('nodes', []),
            'connections': data.get('connections', []),
            'globalNpcs': data.get('globalNpcs', []),
            'selectedProvider': data.get('selectedProvider', 'deepseek'),
            'selectedModel': data.get('selectedModel', 'openai/tngtech/deepseek-r1t2-chimera:free'),
            'useAutoTitle': data.get('useAutoTitle', True)
        }

        try:
            # 동일 이름의 프리셋이 이미 있는지 확인 (같은 사용자)
            existing = Preset.query.filter_by(name=name, author_id=user_id).first()

            if existing:
                # 덮어쓰기
                existing.description = description
                existing.data = preset_data
                existing.updated_at = datetime.utcnow()
                db.session.commit()
                return str(existing.id), None
            else:
                # 새로 생성
                preset = Preset(
                    name=name,
                    description=description,
                    author_id=user_id,
                    data=preset_data
                )
                db.session.add(preset)
                db.session.commit()
                return str(preset.id), None

        except Exception as e:
            db.session.rollback()
            logger.error(f"Preset Save Error: {e}")
            return None, str(e)

    @staticmethod
    def load_preset(preset_id: str, user_id: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        프리셋 로드 (DB ID 기반)

        Returns:
            ({'preset': preset_data}, error_message)
        """
        if not preset_id:
            return None, "ID 누락"

        try:
            db_id = int(preset_id)
            preset = Preset.query.get(db_id)

            if not preset:
                return None, "프리셋을 찾을 수 없습니다."

            # 접근 권한 체크 (본인 프리셋만 로드 가능, 필요시 공개 프리셋 기능 추가 가능)
            if user_id and preset.author_id and preset.author_id != user_id:
                return None, "다른 사용자의 프리셋입니다."

            preset_data = {
                'name': preset.name,
                'description': preset.description,
                'nodes': preset.data.get('nodes', []),
                'connections': preset.data.get('connections', []),
                'globalNpcs': preset.data.get('globalNpcs', []),
                'selectedProvider': preset.data.get('selectedProvider', 'deepseek'),
                'selectedModel': preset.data.get('selectedModel', 'openai/tngtech/deepseek-r1t2-chimera:free'),
                'useAutoTitle': preset.data.get('useAutoTitle', True)
            }

            return {'preset': preset_data}, None

        except ValueError:
            return None, "잘못된 ID 형식입니다."
        except Exception as e:
            logger.error(f"Preset Load Error: {e}")
            return None, str(e)

    @staticmethod
    def delete_preset(preset_id: str, user_id: Optional[str] = None) -> Tuple[bool, Optional[str]]:
        """
        프리셋 삭제 (DB)

        Returns:
            (success, error_message)
        """
        if not preset_id:
            return False, "ID 누락"

        try:
            db_id = int(preset_id)
            preset = Preset.query.get(db_id)

            if not preset:
                return False, "프리셋을 찾을 수 없습니다."

            # 권한 체크
            if user_id and preset.author_id != user_id:
                return False, "삭제 권한이 없습니다."

            db.session.delete(preset)
            db.session.commit()
            return True, None

        except ValueError:
            return False, "잘못된 ID 형식입니다."
        except Exception as e:
            db.session.rollback()
            logger.error(f"Preset Delete Error: {e}")
            return False, str(e)
