import json
import logging

# 필요한 모듈 임포트
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
        2. 실패하거나 설정되지 않은 경우, 확장된 '키워드 분석 규칙'을 통해 답변을 반환합니다.
        """

        # [학습 내용] AI에게 주입할 프로젝트 지식 정보 (LLM 연결 시 사용됨)
        context_text = """
        [TRPG Studio 서비스 정보]
        1. 서비스 개요: 여울(YEOUL)은 멀티 에이전트 AI 기반의 인터랙티브 TRPG 플랫폼입니다.
        2. 시나리오 제작 (Builder Mode): 노드 기반 편집기, AI 보조 도구(NPC/지문 생성), 로직 검수 제공.
        3. 요금제: Free(3개 생성), Pro(9,900원/무제한/GPT-4), Biz(29,900원/파인튜닝).
        4. 플레이: 메인 화면 리스트 선택 -> 1:1 AI GM과 플레이.
        """

        try:
            # 시스템 프롬프트 구성
            system_prompt = """
            당신은 TRPG Studio의 친절한 AI 가이드 '여울'입니다. 
            제공된 정보를 바탕으로 사용자의 질문에 친절하게 답변하세요.
            답변 후에는 사용자가 이어서 질문할 만한 '추가 선택지(choices)'를 2~3개 제안해주세요.

            반드시 아래 JSON 형식을 지켜서 응답하세요. (마크다운 없이 순수 JSON만)
            {
                "answer": "답변 내용...",
                "choices": ["선택지1", "선택지2"]
            }
            """

            # LLM 호출 시도
            if 'LLMFactory' in globals() and hasattr(LLMFactory, 'create_llm'):
                try:
                    llm = LLMFactory.create_llm("gpt-4o")
                    response_text = await llm.chat_completion(
                        system_prompt=system_prompt,
                        user_input=f"Context: {context_text}\n\nQuestion: {user_query}"
                    )
                    cleaned_text = response_text.replace("```json", "").replace("```", "").strip()
                    return json.loads(cleaned_text)
                except Exception as e:
                    logger.warning(f"LLM 호출 실패 (Fallback 전환): {e}")
                    return ChatbotService.get_keyword_response(user_query)
            else:
                return ChatbotService.get_keyword_response(user_query)

        except Exception as e:
            logger.error(f"Chatbot Critical Error: {e}")
            return ChatbotService.get_keyword_response(user_query)

    # ▼▼▼ [확장됨] 다양한 질문에 대응하는 똑똑한 답변 로직 ▼▼▼
    @staticmethod
    def get_keyword_response(query: str) -> dict:
        """
        AI 모델 연결 불가 시, 질문의 핵심 단어를 분석하여 준비된 답변을 제공합니다.
        """
        query = query.lower().strip()  # 소문자 변환 및 공백 제거

        # 1. 인사말 처리
        if any(w in query for w in ['안녕', '반가', 'hi', 'hello', 'ㅎㅇ']):
            return {
                "answer": "안녕하세요! 모험가님. 👋\n저는 TRPG Studio의 안내를 돕는 AI 가이드 '여울'입니다.\n무엇을 도와드릴까요?",
                "choices": ["시나리오 제작 방법", "요금제 안내", "게임 플레이 방법"]
            }

        # 2. 무료 기능 / Adventurer
        if any(w in query for w in ['무료', 'free', 'adventurer', '공짜']):
            return {
                "answer": "🎒 **Adventurer (Free) 플랜**\n\n입문자를 위한 기본 플랜입니다.\n\n✅ **주요 혜택**\n• 시나리오 생성 3개\n• 기본 AI 모델 사용\n• 커뮤니티 접근\n\n부담 없이 TRPG의 세계를 경험해보세요!",
                "choices": ["시나리오 제작 방법", "다른 요금제 보기", "처음으로"]
            }

        # 3. 요금제 / 비용 / 결제
        if any(w in query for w in ['요금', '가격', '비용', '결제', 'plan', '구독', '프로', 'pro']):
            return {
                "answer": "💳 **요금제 안내**\n\n모험가님의 스타일에 맞는 플랜을 선택하세요!\n\n🔹 **Adventurer (Free)**: 무료, 기본 기능\n🔹 **Dungeon Master (9,900원/월)**: 무제한 생성, GPT-4, 이미지 50회\n🔹 **World Creator (29,900원/월)**: 모든 기능 + 전용 파인튜닝 모델\n\n자세한 내용은 마이페이지에서 확인 가능합니다.",
                "choices": ["마이페이지로 이동", "무료 기능 더보기", "처음으로"]
            }

        # 4. 시나리오 제작 / 빌더 / 노드
        if any(w in query for w in ['제작', '만들기', '생성', '빌더', 'create', '노드', '에디터', '편집']):
            return {
                "answer": "🛠️ **시나리오 제작 (Builder Mode)**\n\nTRPG Studio는 **노드(Node) 기반 편집기**를 제공합니다.\n코딩 없이 이야기의 흐름을 시각적으로 연결하여 나만의 모험을 만들 수 있습니다.\n\n상단의 **'Start Creation'** 버튼을 눌러 지금 시작해보세요!",
                "choices": ["빌더 모드 이동", "AI 도구가 뭔가요?", "다른 기능"]
            }

        # 5. AI 도구 / NPC 생성 / 검수
        if any(w in query for w in ['ai', '도구', 'tool', '인공지능', 'npc', '자동', '검수', '기능']):
            return {
                "answer": "🤖 **AI 보조 도구 소개**\n\n창작자를 위한 강력한 AI 기능들을 지원합니다.\n\n1. **NPC 제네레이터**: 이름만 넣으면 성격/배경 자동 생성\n2. **자동 씬 묘사**: 키워드로 몰입감 있는 지문 작성\n3. **로직 검수기**: 시나리오 분기 및 오류 자동 분석\n\n빌더 모드에서 이 기능들을 활용해 보세요.",
                "choices": ["빌더 모드 이동", "시나리오 제작 방법", "처음으로"]
            }

        # 6. 플레이 / 게임 시작
        if any(w in query for w in ['플레이', '게임', '시작', 'play', '하기', '참여']):
            return {
                "answer": "🎮 **게임 플레이 방법**\n\n메인 화면에 있는 다양한 장르(판타지, 스릴러 등)의 시나리오 중 하나를 선택해 보세요.\n**'PLAY'** 버튼을 누르면 AI 게임마스터와 함께 1:1 모험이 시작됩니다.",
                "choices": ["인기 시나리오 추천", "내 시나리오 보기", "처음으로"]
            }

        # 7. 로그인 / 계정 / 회원가입
        if any(w in query for w in ['로그인', '계정', '가입', '아이디', '비번', 'password', 'sign']):
            return {
                "answer": "🔐 **계정 관리**\n\n우측 상단의 **LOGIN** 버튼을 통해 로그인하거나 회원가입할 수 있습니다.\n구글, 카카오, 네이버 소셜 로그인도 지원합니다.\n로그인 후 나만의 시나리오를 저장하고 공유해보세요.",
                "choices": ["마이페이지로 이동", "처음으로"]
            }

        # 8. 이미지 / 그림
        if any(w in query for w in ['이미지', '그림', '일러스트', 'image', 'picture']):
            return {
                "answer": "🎨 **이미지 생성 기능**\n\n시나리오의 상황에 맞는 이미지를 AI가 실시간으로 생성해줍니다.\n이 기능은 **Dungeon Master (Pro)** 플랜 이상에서 월 50회 제공됩니다.",
                "choices": ["요금제 안내", "시나리오 제작 방법"]
            }

        # ▼▼▼ [추가] '처음으로' 키워드 처리 (여기부터 복사해서 붙여넣으세요) ▼▼▼
        if any(w in query for w in ['처음', '시작', 'start', 'home', '메인', 'reset', '리셋']):
            return {
                "answer": "안녕하세요! 모험가님. 👋\n무엇을 도와드릴까요?",
                "choices": ["시나리오 제작 방법", "요금제 안내", "게임 플레이 방법"]
            }

        # 9. 기본 응답 (매칭되는 키워드가 없을 때)
        return {
            "answer": f"죄송합니다. 말씀하신 '{query}'에 대한 정확한 정보를 찾지 못했습니다.\n하지만 아래 메뉴를 통해 도움을 드릴 수 있습니다.",
            "choices": ["시나리오 제작 방법", "요금제 안내", "문의하기"]
        }