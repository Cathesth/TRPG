from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from services.chatbot_service import ChatbotService

router = APIRouter(prefix="/api/chatbot", tags=["chatbot"])

class ChatRequest(BaseModel):
    query: str

class ChatResponse(BaseModel):
    answer: str
    choices: List[str] = []

@router.post("/query", response_model=ChatResponse)
async def query_chatbot(req: ChatRequest):
    """
    RAG 기반 챗봇 질의 엔드포인트
    - 사용자의 질문을 받아 적절한 답변과 추가 선택지를 반환합니다.
    """
    try:
        response = await ChatbotService.generate_response(req.query)
        return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))