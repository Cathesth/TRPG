import json
import logging

# 필요한 모듈 임포트 (파일이 없을 경우를 대비해 try-except 처리)
try:
    from core.vector_db import get_vector_db_client
    from llm_factory import LLMFactory
except ImportError:
    pass

logger = logging.getLogger(__name__)


class ChatbotService:
    @staticmethod
    async def generate_response(user_query: str) -> dict:
        try:
            # 1. RAG: 관련 문서 검색 (구현 시 주석 해제하여 사용)
            # vector_db = get_vector_db_client()
            # context_docs = await vector_db.search(user_query, top_k=3)
            # context_text = "\n".join([doc.content for doc in context_docs])

            # [임시 컨텍스트] 실제 RAG 연동 전까지 사용할 기본 정보
            context_text = """
            TRPG Studio는 AI 기반 시나리오 저작 및 플레이 도구입니다.
            사용자는 노드 기반으로 시나리오를 작성할 수 있으며, 
            플레이어는 AI 게임마스터와 상호작용하며 자유도 높은 플레이를 즐길 수 있습니다.
            기본 요금제는 무료이며, Pro 요금제는 월 9,900원입니다.
            시나리오 제작은 '빌더 모드'에서 가능합니다.
            """

            # 2. 프롬프트 구성 (시스템 프롬프트)
            system_prompt = """
            당신은 TRPG Studio의 친절한 AI 가이드 '여울'입니다. 
            사용자의 질문에 대해 제공된 Context를 바탕으로 명확하고 친절하게 답변하세요.
            답변 후에는 사용자가 이어서 질문할 만한 '추가 선택지(choices)'를 2~3개 제안해주세요.

            반드시 아래 JSON 형식을 지켜서 응답하세요. 마크다운 코드는 포함하지 마세요.
            {
                "answer": "답변 내용...",
                "choices": ["선택지1", "선택지2"]
            }
            """

            # 3. LLM 호출
            # LLMFactory가 있고 create_llm 메서드가 존재하는지 확인
            if 'LLMFactory' in globals() and hasattr(LLMFactory, 'create_llm'):
                try:
                    llm = LLMFactory.create_llm("gpt-4o")
                    response_text = await llm.chat_completion(
                        system_prompt=system_prompt,
                        user_input=f"Context: {context_text}\n\nQuestion: {user_query}"
                    )
                except Exception as llm_error:
                    logger.error(f"LLM Call Failed: {llm_error}")
                    raise llm_error  # 아래 Fallback으로 이동
            else:
                # LLMFactory가 없거나 메서드가 없는 경우 (테스트용 가짜 응답)
                # [수정] 여기가 실행되어 에러 없이 기본 답변이 나갑니다.
                response_text = json.dumps({
                    "answer": f"현재 AI 모델을 연결할 수 없어 기본 응답을 드립니다.\n질문하신 '{user_query}'에 대한 답변은 준비 중입니다.",
                    "choices": ["시나리오 제작", "요금제 안내"]
                }, ensure_ascii=False)

            # [전처리] LLM이 ```json ... ``` 으로 감싸서 줄 경우 제거
            cleaned_text = response_text.replace("```json", "").replace("```", "").strip()

            # 4. JSON 파싱 및 반환
            return json.loads(cleaned_text)

        except Exception as e:
            logger.error(f"Chatbot Error: {e}")
            # 5. [Fallback] 에러 발생 시 기본 응답 반환
            return {
                "answer": "죄송합니다. 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
                "choices": ["처음으로", "문의하기"]
            }