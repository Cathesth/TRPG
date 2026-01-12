"""
프리셋 관리 서비스 (PostgreSQL DB 기반)
"""
import json
import logging
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime

from models import SessionLocal, Preset

logger = logging.getLogger(__name__)


class PresetService:
    """프리셋 DB 관리 서비스"""

    @staticmethod
    def list_presets(sort_order: str = 'newest', user_id: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """프리셋 목록 조회 (DB 기반)"""
        db = SessionLocal()
        try:
            query = db.query(Preset)

            if user_id:
                query = query.filter(Preset.author_id == user_id)

            if sort_order == 'oldest':
                query = query.order_by(Preset.created_at.asc())
            elif sort_order == 'name_asc':
                query = query.order_by(Preset.name.asc())
            elif sort_order == 'name_desc':
                query = query.order_by(Preset.name.desc())
            else:
                query = query.order_by(Preset.created_at.desc())

            if limit:
                query = query.limit(limit)

            presets = query.all()
            preset_infos = []

            for p in presets:
                p_data = p.data or {}

                preset_infos.append({
                    'filename': str(p.id),
                    'id': p.id,
                    'title': p.name,
                    'desc': p.description or '',
                    'author': p.author_id or 'Anonymous',
                    'is_owner': (user_id is not None) and (p.author_id == user_id),
                    'nodeCount': len(p_data.get('nodes', [])),
                    'npcCount': len(p_data.get('globalNpcs', [])),
                    'model': p_data.get('selectedModel', ''),
                    'createdAt': p.created_at.timestamp() if p.created_at else 0
                })

            return preset_infos
        finally:
            db.close()

    @staticmethod
    def save_preset(data: Dict[str, Any], user_id: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
        """프리셋 저장 (DB)"""
        name = data.get('name', '').strip()
        if not name:
            return None, "프리셋 이름을 입력하세요"

        description = data.get('description', '')

        preset_data = {
            'nodes': data.get('nodes', []),
            'connections': data.get('connections', []),
            'globalNpcs': data.get('globalNpcs', []),
            'selectedProvider': data.get('selectedProvider', 'deepseek'),
            'selectedModel': data.get('selectedModel', 'openai/tngtech/deepseek-r1t2-chimera:free'),
            'useAutoTitle': data.get('useAutoTitle', True)
        }

        db = SessionLocal()
        try:
            existing = db.query(Preset).filter(Preset.name == name, Preset.author_id == user_id).first()

            if existing:
                existing.description = description
                existing.data = preset_data
                existing.updated_at = datetime.now()
                db.commit()
                return str(existing.id), None
            else:
                preset = Preset(
                    name=name,
                    description=description,
                    author_id=user_id,
                    data=preset_data
                )
                db.add(preset)
                db.commit()
                db.refresh(preset)
                return str(preset.id), None

        except Exception as e:
            db.rollback()
            logger.error(f"Preset Save Error: {e}")
            return None, str(e)
        finally:
            db.close()

    @staticmethod
    def load_preset(preset_id: str, user_id: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """프리셋 로드 (DB ID 기반)"""
        if not preset_id:
            return None, "ID 누락"

        db = SessionLocal()
        try:
            db_id = int(preset_id)
            preset = db.query(Preset).filter(Preset.id == db_id).first()

            if not preset:
                return None, "프리셋을 찾을 수 없습니다."

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
        finally:
            db.close()

    @staticmethod
    def delete_preset(preset_id: str, user_id: Optional[str] = None) -> Tuple[bool, Optional[str]]:
        """프리셋 삭제 (DB)"""
        if not preset_id:
            return False, "ID 누락"

        db = SessionLocal()
        try:
            db_id = int(preset_id)
            preset = db.query(Preset).filter(Preset.id == db_id).first()

            if not preset:
                return False, "프리셋을 찾을 수 없습니다."

            if user_id and preset.author_id != user_id:
                return False, "삭제 권한이 없습니다."

            db.delete(preset)
            db.commit()
            return True, None

        except ValueError:
            return False, "잘못된 ID 형식입니다."
        except Exception as e:
            db.rollback()
            logger.error(f"Preset Delete Error: {e}")
            return False, str(e)
        finally:
            db.close()
