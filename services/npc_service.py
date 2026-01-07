import json
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# DB 경로 설정
DB_DIR = os.path.join(os.getcwd(), 'DB')
NPC_FILE = os.path.join(DB_DIR, 'custom_npcs.json')


def _ensure_db_exists():
    """DB 디렉토리와 파일이 없으면 생성"""
    if not os.path.exists(DB_DIR):
        os.makedirs(DB_DIR)

    if not os.path.exists(NPC_FILE):
        with open(NPC_FILE, 'w', encoding='utf-8') as f:
            json.dump([], f, ensure_ascii=False, indent=2)


def load_custom_npcs():
    """저장된 모든 커스텀 NPC/Enemy 로드"""
    _ensure_db_exists()
    try:
        with open(NPC_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load NPCs: {e}")
        return []


def save_custom_npc(npc_data: dict):
    """NPC/Enemy 데이터를 파일에 추가 저장"""
    _ensure_db_exists()

    # 필수 필드 보정
    npc_data['id'] = npc_data.get('id') or f"npc_{int(datetime.now().timestamp())}"
    npc_data['created_at'] = datetime.now().isoformat()

    try:
        # 기존 데이터 로드
        current_list = load_custom_npcs()

        # 중복 체크 (이름 기준 덮어쓰기 or 추가)
        # 여기서는 단순 추가로 구현하되, ID가 같으면 업데이트
        updated = False
        for idx, item in enumerate(current_list):
            if item.get('name') == npc_data.get('name') and item.get('isEnemy') == npc_data.get('isEnemy'):
                current_list[idx] = npc_data
                updated = True
                break

        if not updated:
            current_list.append(npc_data)

        # 파일 저장
        with open(NPC_FILE, 'w', encoding='utf-8') as f:
            json.dump(current_list, f, ensure_ascii=False, indent=2)

        logger.info(f"NPC Saved: {npc_data.get('name')}")
        return npc_data

    except Exception as e:
        logger.error(f"Failed to save NPC: {e}")
        raise e