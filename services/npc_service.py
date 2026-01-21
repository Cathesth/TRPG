import logging
from models import SessionLocal, CustomNPC
from core.vector_db import get_vector_db_client
from datetime import datetime
from typing import Optional, List, Dict, Any

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


async def save_npc_conversation(
    npc_id: int,
    scenario_id: int,
    user_message: str,
    npc_response: str,
    context: Optional[str] = None
) -> bool:
    """
    NPC와의 대화를 Vector DB에 저장

    Args:
        npc_id: NPC의 DB ID
        scenario_id: 시나리오 ID
        user_message: 플레이어의 메시지
        npc_response: NPC의 응답
        context: 대화 컨텍스트 (선택)

    Returns:
        성공 여부
    """
    vector_db = get_vector_db_client()

    if not vector_db.is_available:
        logger.warning("⚠️ Vector DB가 비활성화되어 있어 대화 저장을 건너뜁니다.")
        return False

    try:
        # 대화 내용을 하나의 텍스트로 결합
        conversation_text = f"플레이어: {user_message}\nNPC: {npc_response}"
        if context:
            conversation_text = f"상황: {context}\n{conversation_text}"

        # 메타데이터 준비
        metadata = {
            "timestamp": datetime.now().isoformat(),
            "event_type": "conversation",
            "user_message": user_message,
            "npc_response": npc_response,
            "context": context
        }

        # Vector DB에 저장
        success = await vector_db.upsert_memory(
            npc_id=npc_id,
            scenario_id=scenario_id,
            text=conversation_text,
            metadata=metadata
        )

        if success:
            logger.info(f"💬 [NPC Memory] 대화 저장 완료: NPC={npc_id}, Scenario={scenario_id}")

        return success

    except Exception as e:
        logger.error(f"❌ [NPC Memory] 대화 저장 실패: {e}")
        return False


async def search_npc_memories(
    npc_id: int,
    query: str,
    scenario_id: Optional[int] = None,
    limit: int = 5
) -> List[Dict[str, Any]]:
    """
    NPC의 과거 대화/기억 검색

    Args:
        npc_id: NPC ID
        query: 검색 쿼리 (자연어)
        scenario_id: 시나리오 ID (선택)
        limit: 최대 결과 수

    Returns:
        관련 대화 기록 리스트
    """
    vector_db = get_vector_db_client()

    if not vector_db.is_available:
        logger.warning("⚠️ Vector DB가 비활성화되어 있어 기억 검색을 건너뜁니다.")
        return []

    try:
        results = await vector_db.search_memory(
            query=query,
            npc_id=npc_id,
            scenario_id=scenario_id,
            limit=limit
        )

        logger.info(f"🔍 [NPC Memory] {len(results)}개의 관련 기억 검색 완료")
        return results

    except Exception as e:
        logger.error(f"❌ [NPC Memory] 기억 검색 실패: {e}")
        return []


async def save_npc_lore(
    npc_id: int,
    scenario_id: int,
    lore_text: str,
    lore_type: str = "background"
) -> bool:
    """
    NPC의 배경 설정/설정 정보를 Vector DB에 저장

    Args:
        npc_id: NPC ID
        scenario_id: 시나리오 ID
        lore_text: 설정 텍스트 (배경, 성격, 목표 등)
        lore_type: 설정 유형 (background, personality, goal 등)

    Returns:
        성공 여부
    """
    vector_db = get_vector_db_client()

    if not vector_db.is_available:
        return False

    try:
        metadata = {
            "timestamp": datetime.now().isoformat(),
            "event_type": "lore",
            "lore_type": lore_type
        }

        success = await vector_db.upsert_memory(
            npc_id=npc_id,
            scenario_id=scenario_id,
            text=lore_text,
            metadata=metadata
        )

        if success:
            logger.info(f"📖 [NPC Lore] 설정 저장 완료: NPC={npc_id}, Type={lore_type}")

        return success

    except Exception as e:
        logger.error(f"❌ [NPC Lore] 설정 저장 실패: {e}")
        return False


async def get_npc_context_for_ai(
    npc_id: int,
    current_situation: str,
    scenario_id: Optional[int] = None,
    memory_limit: int = 3
) -> str:
    """
    AI 프롬프트에 사용할 NPC의 관련 기억 컨텍스트 생성

    Args:
        npc_id: NPC ID
        current_situation: 현재 상황 설명
        scenario_id: 시나리오 ID
        memory_limit: 가져올 기억 개수

    Returns:
        프롬프트에 삽입할 컨텍스트 문자열
    """
    memories = await search_npc_memories(
        npc_id=npc_id,
        query=current_situation,
        scenario_id=scenario_id,
        limit=memory_limit
    )

    if not memories:
        return ""

    context_parts = ["[NPC의 관련 기억]"]
    for i, memory in enumerate(memories, 1):
        context_parts.append(f"{i}. {memory['text']} (관련도: {memory['score']:.2f})")

    return "\n".join(context_parts)
