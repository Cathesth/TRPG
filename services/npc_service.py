import logging
from models import SessionLocal, CustomNPC

logger = logging.getLogger(__name__)


def save_custom_npc(data: dict, user_id: str = None):
    """
    NPC/Enemy 데이터를 DB에 저장합니다.
    """
    db = SessionLocal()
    try:
        # 데이터 정제
        name = data.get('name', 'Unknown')
        npc_type = 'enemy' if data.get('isEnemy') else 'npc'

        # 새로운 NPC 객체 생성
        new_npc = CustomNPC(
            name=name,
            type=npc_type,
            data=data,  # JSON 데이터 통째로 저장
            author_id=user_id
        )

        db.add(new_npc)
        db.commit()
        db.refresh(new_npc)

        logger.info(f"Custom NPC Saved: {name} (ID: {new_npc.id})")

        # 저장된 데이터 반환 (ID 포함)
        return new_npc.to_dict()

    except Exception as e:
        db.rollback()
        logger.error(f"Failed to save NPC to DB: {e}")
        raise e
    finally:
        db.close()


def load_custom_npcs(user_id=None):
    """
    저장된 NPC 목록을 불러옵니다.
    """
    db = SessionLocal()
    try:
        query = db.query(CustomNPC)

        # 로그인한 유저의 NPC만 가져오기 (원한다면)
        if user_id:
            query = query.filter(CustomNPC.author_id == user_id)

        npcs = query.order_by(CustomNPC.created_at.desc()).all()

        # 프론트엔드에서 사용하는 포맷인 data 필드 안의 내용을 반환하되, id 등을 주입
        result = []
        for npc in npcs:
            npc_dict = npc.data.copy() if npc.data else {}
            npc_dict['db_id'] = npc.id  # DB 상의 ID 식별자 추가
            result.append(npc_dict)

        return result

    except Exception as e:
        logger.error(f"Failed to load NPCs from DB: {e}")
        return []
    finally:
        db.close()
