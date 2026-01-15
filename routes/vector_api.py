"""
Vector DB API 엔드포인트
NPC 기억 저장 및 검색 기능
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, List
import logging

from services.npc_service import (
    save_npc_conversation,
    search_npc_memories,
    save_npc_lore,
    get_npc_context_for_ai
)

router = APIRouter(prefix="/api/vector", tags=["Vector DB"])
logger = logging.getLogger(__name__)


class ConversationRequest(BaseModel):
    npc_id: int
    scenario_id: int
    user_message: str
    npc_response: str
    context: Optional[str] = None


class MemorySearchRequest(BaseModel):
    npc_id: int
    query: str
    scenario_id: Optional[int] = None
    limit: int = 5


class LoreRequest(BaseModel):
    npc_id: int
    scenario_id: int
    lore_text: str
    lore_type: str = "background"


class ContextRequest(BaseModel):
    npc_id: int
    current_situation: str
    scenario_id: Optional[int] = None
    memory_limit: int = 3


@router.post("/save-conversation")
async def api_save_conversation(request: ConversationRequest):
    """
    NPC와의 대화를 Vector DB에 저장

    **사용 예시:**
    ```json
    {
        "npc_id": 1,
        "scenario_id": 10,
        "user_message": "당신은 누구신가요?",
        "npc_response": "나는 이 마을의 대장장이야.",
        "context": "플레이어가 마을에 처음 도착했다."
    }
    ```
    """
    try:
        success = await save_npc_conversation(
            npc_id=request.npc_id,
            scenario_id=request.scenario_id,
            user_message=request.user_message,
            npc_response=request.npc_response,
            context=request.context
        )

        if not success:
            return JSONResponse(
                status_code=200,
                content={
                    "success": False,
                    "message": "Vector DB가 비활성화되어 있거나 저장에 실패했습니다."
                }
            )

        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "message": "대화가 성공적으로 저장되었습니다.",
                "npc_id": request.npc_id,
                "scenario_id": request.scenario_id
            }
        )

    except Exception as e:
        logger.error(f"❌ [API] 대화 저장 중 오류: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/search-memories")
async def api_search_memories(request: MemorySearchRequest):
    """
    NPC의 과거 기억 검색

    **사용 예시:**
    ```json
    {
        "npc_id": 1,
        "query": "대장간에서 무기를 만드는 방법",
        "scenario_id": 10,
        "limit": 5
    }
    ```

    **응답 예시:**
    ```json
    {
        "success": true,
        "memories": [
            {
                "score": 0.89,
                "text": "플레이어: 무기는 어떻게 만드나요?\nNPC: 좋은 철과 불, 그리고 인내심이 필요하지.",
                "npc_id": 1,
                "scenario_id": 10,
                "metadata": {
                    "timestamp": "2026-01-15T10:30:00",
                    "event_type": "conversation"
                }
            }
        ],
        "count": 1
    }
    ```
    """
    try:
        memories = await search_npc_memories(
            npc_id=request.npc_id,
            query=request.query,
            scenario_id=request.scenario_id,
            limit=request.limit
        )

        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "memories": memories,
                "count": len(memories)
            }
        )

    except Exception as e:
        logger.error(f"❌ [API] 기억 검색 중 오류: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/save-lore")
async def api_save_lore(request: LoreRequest):
    """
    NPC의 배경 설정/설정 정보 저장

    **사용 예시:**
    ```json
    {
        "npc_id": 1,
        "scenario_id": 10,
        "lore_text": "이 대장장이는 50년 경력의 베테랑으로, 전설적인 검을 만든 적이 있다.",
        "lore_type": "background"
    }
    ```
    """
    try:
        success = await save_npc_lore(
            npc_id=request.npc_id,
            scenario_id=request.scenario_id,
            lore_text=request.lore_text,
            lore_type=request.lore_type
        )

        if not success:
            return JSONResponse(
                status_code=200,
                content={
                    "success": False,
                    "message": "Vector DB가 비활성화되어 있거나 저장에 실패했습니다."
                }
            )

        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "message": "설정이 성공적으로 저장되었습니다."
            }
        )

    except Exception as e:
        logger.error(f"❌ [API] 설정 저장 중 오류: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/get-ai-context")
async def api_get_ai_context(request: ContextRequest):
    """
    AI 프롬프트에 사용할 NPC 컨텍스트 생성

    **사용 예시:**
    ```json
    {
        "npc_id": 1,
        "current_situation": "플레이어가 전설의 검에 대해 물어본다",
        "scenario_id": 10,
        "memory_limit": 3
    }
    ```

    **응답 예시:**
    ```json
    {
        "success": true,
        "context": "[NPC의 관련 기억]\n1. 이 대장장이는 전설적인 검을 만든 적이 있다. (관련도: 0.92)\n2. 플레이어: 당신의 작품을 보여주세요...",
        "memory_count": 3
    }
    ```
    """
    try:
        context = await get_npc_context_for_ai(
            npc_id=request.npc_id,
            current_situation=request.current_situation,
            scenario_id=request.scenario_id,
            memory_limit=request.memory_limit
        )

        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "context": context,
                "memory_count": len(context.split("\n")) - 1 if context else 0
            }
        )

    except Exception as e:
        logger.error(f"❌ [API] 컨텍스트 생성 중 오류: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health")
async def check_vector_db_health():
    """Vector DB 상태 확인"""
    from core.vector_db import get_vector_db_client

    vector_db = get_vector_db_client()

    return {
        "vector_db_available": vector_db.is_available,
        "vector_db_initialized": vector_db._initialized,
        "collection_name": vector_db.collection_name if vector_db.is_available else None,
        "qdrant_url": vector_db.qdrant_url if vector_db.is_available else None,
        "openai_configured": vector_db.openai_client is not None
    }

