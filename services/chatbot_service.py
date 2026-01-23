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
        """
        사용자의 질문을 받아 답변을 생성합니다.
        1. LLM(AI) 연결을 시도합니다.
        2. 실패하거나 설정되지 않은 경우, '키워드 분석 규칙'을 통해 정해진 답변을 반환합니다.
        """

        # ▼▼▼ [학습 내용] AI에게 주입할 프로젝트 지식 정보 ▼▼▼
        context_text = """
        [TRPG Studio 서비스 정보]
        1. 서비스 개요: 여울(YEOUL)은 멀티 에이전트 AI 기반의 인터랙티브 TRPG 플랫폼입니다.

        2. 시나리오 제작 (Builder Mode):
           - 메인 화면의 'Start Creation' 또는 'Builder' 버튼을 눌러 접속합니다.
           - '노드(Node) 기반 편집기'를 제공하여 코딩 없이 드래그 앤 드롭으로 흐름을 만듭니다.
           - 'AI 보조 도구'를 사용하면 NPC 대사, 상황 묘사, 선택지를 AI가 자동으로 작성해줍니다.
           - 작성된 시나리오가 논리적으로 맞는지 'AI 검수' 기능을 제공합니다.

        3. 요금제 안내 (Pricing):
           - Adventurer (Free): 무료. 시나리오 생성 3개 제한, 기본 AI 모델 사용.
           - Dungeon Master (Pro): 월 9,900원. 시나리오 무제한, GPT-4 등 고급 모델, 이미지 생성 50회.
           - World Creator (Biz): 월 29,900원. 모든 기능 포함, 전용 파인튜닝 모델 제공.

        4. 플레이 방법:
           - 메인 화면에서 원하는 장르(판타지, 스릴러 등)의 시나리오를 선택해 'PLAY' 버튼을 누릅니다.
           - AI 게임마스터(GM)와 1:1로 대화하며 자유도 높은 플레이가 가능합니다.
        """

        try:
            # 2. 시스템 프롬프트 구성
            system_prompt = """
            당신은 TRPG Studio의 친절한 AI 가이드 '여울'입니다. 
            제공된 [TRPG Studio 서비스 정보]를 바탕으로 사용자의 질문에 친절하게 답변하세요.
            답변 후에는 사용자가 이어서 질문할 만한 '추가 선택지(choices)'를 2~3개 제안해주세요.

            반드시 아래 JSON 형식을 지켜서 응답하세요. (마크다운 없이 순수 JSON만)
            {
                "answer": "질문에 대한 답변 내용...",
                "choices": ["선택지1", "선택지2"]
            }
            """

            # 3. LLM 호출 시도
            # LLMFactory가 있고, create_llm 메서드가 실제로 존재하는지 체크
            if 'LLMFactory' in globals() and hasattr(LLMFactory, 'create_llm'):
                try:
                    llm = LLMFactory.create_llm("gpt-4o")
                    response_text = await llm.chat_completion(
                        system_prompt=system_prompt,
                        user_input=f"Context: {context_text}\n\nQuestion: {user_query}"
                    )

                    # JSON 전처리 및 반환
                    cleaned_text = response_text.replace("```json", "").replace("```", "").strip()
                    return json.loads(cleaned_text)

                except Exception as e:
                    logger.warning(f"LLM 호출 실패 (Fallback 전환): {e}")
                    # LLM 실패 시 아래 '키워드 분석 로직'으로 넘어감
                    return ChatbotService.get_keyword_response(user_query)
            else:
                # LLMFactory가 없는 경우 바로 키워드 분석
                return ChatbotService.get_keyword_response(user_query)

        except Exception as e:
            logger.error(f"Chatbot Critical Error: {e}")
            # 최후의 수단
            return ChatbotService.get_keyword_response(user_query)

    # ▼▼▼ [추가] AI 연결 실패 시 작동하는 '똑똑한 대체 로직' ▼▼▼
    @staticmethod
    def get_keyword_response(query: str) -> dict:
        """
        AI 모델 연결이 안 될 때, 질문에 포함된 단어를 보고
        미리 준비된 답변을 찾아주는 함수입니다.
        """
        query = query.lower()  # 소문자로 변환하여 비교

        # 1. 시나리오 제작 관련 질문
        if any(word in query for word in ['제작', '만들기', '생성', '빌더', 'create']):
            return {
                "answer": "🛠️ **시나리오 제작 방법**\n\nTRPG Studio의 **'빌더 모드'**를 통해 나만의 이야기를 만들 수 있습니다.\n\n1. 메인 화면의 **'Start Creation'** 버튼을 클릭하세요.\n2. **노드 기반 에디터**에서 스토리의 흐름을 시각적으로 연결합니다.\n3. **AI 생성 도구**를 사용하면 NPC 대사와 지문을 자동으로 작성할 수 있습니다.\n\n지금 바로 시작해보시겠어요?",
                "choices": ["빌더 모드 이동", "AI 도구가 뭔가요?", "다른 기능"]
            }

        # 2. 요금제 관련 질문
        elif any(word in query for word in ['요금', '가격', '비용', '무료', '결제', 'plan']):
            return {
                "answer": "💳 **요금제 안내**\n\n모험가님의 스타일에 맞는 플랜을 선택하세요!\n\n🔹 **Adventurer (Free)**: 무료, 시나리오 3개 생성 가능\n🔹 **Dungeon Master (9,900원/월)**: 무제한 생성, GPT-4 모델, 이미지 생성 50회\n🔹 **World Creator (29,900원/월)**: 모든 기능 무제한 + 전용 파인튜닝 모델\n\n자세한 내용은 마이페이지에서 확인 가능합니다.",
                "choices": ["마이페이지로 이동", "무료 기능 더보기", "처음으로"]
            }

        # 3. 플레이 관련 질문
        elif any(word in query for word in ['플레이', '게임', '시작', 'play']):
            return {
                "answer": "🎮 **게임 플레이 방법**\n\n메인 화면에 있는 다양한 장르(판타지, 스릴러 등)의 시나리오 중 하나를 선택해 보세요. **'PLAY'** 버튼을 누르면 AI 게임마스터와 함께 1:1 모험이 시작됩니다.",
                "choices": ["인기 시나리오 추천", "내 시나리오 보기", "처음으로"]
            }

        # 4. 기본 응답 (키워드를 못 찾았을 때)
        else:
            return {
                "answer": f"죄송합니다, 현재 AI 통신 상태가 원활하지 않아 '{query}'에 대한 정확한 답변을 생성하지 못했습니다.\n하지만 아래 메뉴를 통해 도움을 드릴 수 있습니다.",
                "choices": ["시나리오 제작 방법", "요금제 안내", "게임 플레이 방법"]
            }