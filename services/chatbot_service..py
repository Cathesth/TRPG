import json
from core.vector_db import get_vector_db_client  # 기존 벡터 DB 활용 가정
from llm_factory import LLMFactory  # 기존 LLM 팩토리 활용 가정


class ChatbotService:
    @staticmethod
    async def generate_response(user_query: str) -> dict:
        # 1. RAG: 관련 문서 검색 (예시)
        # vector_db = get_vector_db_client()
        # context_docs = await vector_db.search(user_query, top_k=3)
        # context_text = "\n".join([doc.content for doc in context_docs])
        # [임시 로직] 실제 RAG/LLM 연동 전 테스트용 응답
        return {
            "answer": f"안녕하세요! 질문하신 '{user_query}'에 대한 AI 가이드의 답변입니다. (현재 데모 모드)",
            "choices": ["시나리오 생성법", "요금제 문의", "기타 질문"]
        }

        context_text = "TRPG Studio는 AI 기반 시나리오 저작 도구입니다..."  # (임시 컨텍스트)

        # 2. 프롬프트 구성 (답변 + 선택지 JSON 포맷 유도)
        system_prompt = """
        당신은 TRPG Studio의 친절한 AI 가이드입니다. 
        사용자의 질문에 대해 제공된 context를 바탕으로 답변하세요.
        답변 후에는 사용자가 이어서 질문할 만한 '추가 선택지(choices)'를 2~3개 제안해주세요.

        응답 형식(JSON):
        {
            "answer": "친절한 답변 내용...",
            "choices": ["선택지1", "선택지2"]
        }
        """

        # 3. LLM 호출
        llm = LLMFactory.create_llm("gpt-4o")  # 또는 설정된 모델
        response_text = await llm.chat_completion(
            system_prompt=system_prompt,
            user_input=f"Context: {context_text}\n\nQuestion: {user_query}"
        )

        # 4. JSON 파싱 및 반환
        try:
            # LLM이 JSON 문자열을 반환한다고 가정
            return json.loads(response_text)
        except:
            # 파싱 실패 시 일반 텍스트로 처리
            return {
                "answer": response_text,
                "choices": ["문의하기", "처음으로"]
            }