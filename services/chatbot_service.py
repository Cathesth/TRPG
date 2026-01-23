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

        [빌더 모드 상세 가이드 - Scene 에디터]
        * Scene 추가: 캔버스 우클릭 또는 상단 '+' 버튼으로 노드 생성.
        * 제목(Title): 씬의 핵심 주제 입력. 플레이어에게 노출됨.
        * 내용(Content): 상황 묘사, 대사 등 메인 텍스트 작성. Markdown 지원.
        * 진입 조건(Entry Condition): 이전 씬에서의 선택지나 변수 상태에 따라 진입 여부 결정.
        * NPC 추가: '등장인물' 탭에서 NPC 생성. 이름/직업 입력 시 AI가 성격 자동 생성.
        * 적(Enemy) 추가: '전투' 탭에서 적 유닛 배치. 스탯(HP/ATK) 설정 가능.
        * 아이템(Item) 추가: 획득 가능한 아이템 설정. 인벤토리 연동.
        * AI 제안 노트(AI Note): 작성 중 막힐 때 AI가 다음 전개를 추천해주는 브레인스토밍 도구.
        * 씬 배경(Background): 이미지 URL 입력 또는 AI 이미지 생성 기능 사용.
        * 이미지 생성(Image Gen): 씬 내용을 분석하여 어울리는 배경/삽화를 AI가 그려줌.
        
        [계정 관리 가이드]
        * 위치: 우측 상단 프로필 아이콘 클릭 -> '마이페이지' 이동.
        * 비밀번호 수정: 마이페이지 -> 좌측 '프로필 수정' 탭 -> 비밀번호 변경 섹션.
        * 회원 탈퇴: 마이페이지 -> 좌측 '프로필 수정' 탭 -> 화면 최하단 '회원 탈퇴' 버튼 (주의: 복구 불가).
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

    # ▼▼▼ [확장됨] 키워드 분석 로직 (빌더 관련 상세 키워드 추가) ▼▼▼
    @staticmethod
    def get_keyword_response(query: str) -> dict:
        """
        AI 모델 연결 불가 시, 질문의 핵심 단어를 분석하여 준비된 답변을 제공합니다.
        """
        query = query.lower().strip()  # 소문자 변환 및 공백 제거

        # 0. 초기화 / 인사
        if any(w in query for w in ['처음', '시작', 'start', 'home', '메인', 'reset', '리셋', '안녕', '반가', 'hi']):
            return {
                "answer": "안녕하세요! 모험가님. 👋\n저는 TRPG Studio의 안내를 돕는 AI 가이드 '여울'입니다.\n무엇을 도와드릴까요?",
                "choices": ["시나리오 제작 방법", "요금제 안내", "게임 플레이 방법"]
            }

        # 1. 계정 관리 (비밀번호/탈퇴/수정) [NEW]
        if any(w in query for w in ['탈퇴', '비밀번호', '비번', 'password', '수정', '변경', '프로필', 'account']):
            return {
                "answer": "🔐 **계정 관리 안내**\n\n회원 탈퇴 및 비밀번호 수정은 **마이페이지**에서 가능합니다.\n\n1. 우측 상단 프로필 클릭 > **마이페이지** 이동\n2. 좌측 메뉴에서 **'프로필 수정'** 클릭\n3. 해당 화면에서 비밀번호 변경 및 회원 탈퇴(하단)를 하실 수 있습니다.",
                "choices": ["마이페이지로 이동", "처음으로"]
            }

        # 1. 무료 기능
        if any(w in query for w in ['무료', 'free', 'adventurer', '공짜']):
            return {
                "answer": "🎒 **Adventurer (Free) 플랜**\n\n입문자를 위한 기본 플랜입니다.\n\n✅ **주요 혜택**\n• 시나리오 생성 3개\n• 기본 AI 모델 사용\n• 커뮤니티 접근\n\n부담 없이 TRPG의 세계를 경험해보세요!",
                "choices": ["시나리오 제작 방법", "다른 요금제 보기", "처음으로"]
            }

        # 2. 요금제
        if any(w in query for w in ['요금', '가격', '비용', '결제', 'plan', '구독']):
            return {
                "answer": "💳 **요금제 안내**\n\n모험가님의 스타일에 맞는 플랜을 선택하세요!\n\n🔹 **Adventurer (Free)**: 무료, 기본 기능\n🔹 **Dungeon Master (9,900원/월)**: 무제한 생성, GPT-4, 이미지 50회\n🔹 **World Creator (29,900원/월)**: 모든 기능 + 전용 파인튜닝 모델\n\n자세한 내용은 마이페이지에서 확인 가능합니다.",
                "choices": ["마이페이지로 이동", "무료 기능 더보기", "처음으로"]
            }

        # 3. [수정] 시나리오 제작 / 빌더 기초
        if any(w in query for w in ['제작', '만들기', '생성', '빌더', 'create', '노드']):
            return {
                "answer": "🛠️ **시나리오 제작 (Builder Mode)**\n\nTRPG Studio는 **노드(Node) 기반 편집기**를 제공합니다.\n코딩 없이 이야기의 흐름을 시각적으로 연결하여 나만의 모험을 만들 수 있습니다.\n\n상단의 **'Start Creation'** 버튼을 눌러 캔버스를 열어보세요!",
                "choices": ["빌더 모드 이동", "씬 추가 방법", "AI 도구가 뭔가요?"]
            }

        # 빌더 상세 (씬/NPC/아이템 등)
        if any(w in query for w in ['씬', 'scene', 'npc', '적', '아이템', '진입', '조건', '배경', '이미지']):
            return {
                "answer": "📚 **빌더 기능 가이드**\n\n• **씬 추가**: 캔버스 우클릭 또는 상단 '+' 버튼\n• **오브젝트(NPC/적)**: 우측 패널 탭에서 생성 및 관리\n• **진입 조건**: 이전 선택지나 아이템 획득 여부 등 설정\n• **이미지 생성**: 씬 내용을 분석해 AI가 배경 삽화 생성",
                "choices": ["빌더 모드 이동", "AI 제안 노트", "처음으로"]
            }

        # ▼▼▼ [신규] 빌더 상세 기능 키워드 처리 ▼▼▼

        # 4-1. 씬 추가/제목/내용
        if any(w in query for w in ['씬', 'scene', '추가', '제목', '내용', '본문']):
            return {
                "answer": "🎬 **Scene(장면) 편집 가이드**\n\n1. **추가**: 캔버스 빈 곳을 우클릭하거나 상단 '+' 버튼을 누르세요.\n2. **제목**: 씬의 핵심 주제를 입력합니다 (플레이어에게 보임).\n3. **내용**: 구체적인 상황 묘사와 대사를 작성하세요. Markdown 문법을 지원합니다.",
                "choices": ["NPC 추가 방법", "배경 이미지 설정", "빌더 모드 이동"]
            }

        # 4-2. NPC/적/아이템
        if any(w in query for w in ['npc', '적', 'enemy', '몬스터', '아이템', 'item', '등장인물']):
            return {
                "answer": "👥 **오브젝트 관리 (NPC/적/아이템)**\n\n씬 에디터 우측 패널에서 탭을 선택하세요.\n\n• **NPC**: 이름/직업만 넣으면 AI가 성격과 말투를 자동 생성합니다.\n• **적(Enemy)**: 전투 발생 시 등장할 몬스터의 스탯(HP/ATK)을 설정합니다.\n• **아이템**: 플레이어가 이 씬에서 획득할 보상을 설정합니다.",
                "choices": ["AI 도구가 뭔가요?", "진입 조건 설정", "빌더 모드 이동"]
            }

        # 4-3. 진입 조건 / 분기
        if any(w in query for w in ['진입', '조건', '분기', '선택지', '연결']):
            return {
                "answer": "🔀 **진입 조건 (Entry Condition)**\n\n스토리의 개연성을 위한 기능입니다.\n이전 씬에서의 **선택지(Choice)** 결과나, 특정 **아이템 보유 여부** 등을 조건으로 걸 수 있습니다.\n조건이 맞지 않으면 해당 씬으로 넘어가지 않습니다.",
                "choices": ["씬 추가 방법", "AI 제안 노트", "처음으로"]
            }

        # 4-4. 배경 / 이미지 생성
        if any(w in query for w in ['배경', '이미지', '그림', 'image', 'background', '삽화']):
            return {
                "answer": "🎨 **배경 및 이미지 생성**\n\n씬의 몰입도를 높여보세요!\n\n1. **URL 입력**: 외부 이미지 주소를 직접 넣을 수 있습니다.\n2. **AI 생성**: '이미지 생성' 버튼을 누르면, 현재 씬의 내용을 AI가 분석하여 어울리는 삽화를 즉석에서 그려줍니다. (Pro 플랜 이상)",
                "choices": ["요금제 안내", "AI 제안 노트", "빌더 모드 이동"]
            }

        # 4-5. AI 제안 / 노트
        if any(w in query for w in ['제안', '노트', 'note', '아이디어', '추천', '브레인']):
            return {
                "answer": "💡 **AI 제안 노트 (AI Note)**\n\n스토리가 막힐 때 사용하세요!\n작성 중인 내용을 바탕으로 AI가 **다음 전개, 반전 요소, 대사** 등을 브레인스토밍해줍니다.\n마음에 드는 내용은 클릭 한 번으로 시나리오에 반영할 수 있습니다.",
                "choices": ["NPC 추가 방법", "이미지 생성", "처음으로"]
            }
        # ▲▲▲ [신규 추가 끝] ▲▲▲

        # 5. AI 도구 (일반)
        if any(w in query for w in ['ai', '도구', 'tool', '인공지능', '기능']):
            return {
                "answer": "🤖 **AI 보조 도구 소개**\n\nTRPG Studio는 창작자를 위한 강력한 AI 도구들을 제공합니다.\n\n1. **NPC 제네레이터**: 성격/배경 자동 생성\n2. **자동 씬 묘사**: 키워드로 지문 작성\n3. **로직 검수기**: 오류 자동 분석\n\n빌더 모드에서 이 기능들을 체험해보세요!",
                "choices": ["빌더 모드 이동", "시나리오 제작 방법", "처음으로"]
            }

        # 6. 인기/추천 시나리오 로직
        if any(w in query for w in ['인기', '추천', '랭킹', '순위', 'popular', 'top', '1위']):
            try:
                # 1. DB 세션 생성
                from models import get_db, Scenario, ScenarioLike
                from sqlalchemy import func
                db = next(get_db())

                # 2. 인기순 정렬 쿼리
                top_scenario = db.query(Scenario).filter(Scenario.is_public == True) \
                    .outerjoin(ScenarioLike, Scenario.id == ScenarioLike.scenario_id) \
                    .group_by(Scenario.id) \
                    .order_by(
                    (func.count(ScenarioLike.scenario_id) * 10 + func.coalesce(Scenario.view_count, 0)).desc()) \
                    .first()

                if top_scenario:
                    # 데이터 파싱
                    s_data = top_scenario.data if isinstance(top_scenario.data, dict) else {}
                    inner = s_data.get('scenario', s_data)
                    title = top_scenario.title or "제목 없음"
                    desc = inner.get('prologue', inner.get('desc', '설명이 없습니다.'))
                    if len(desc) > 80: desc = desc[:80] + "..."

                    answer_text = (
                        f"🏆 **현재 인기 1위 시나리오**\n\n"
                        f"✨ **{title}**\n"
                        f"📖 {desc}\n\n"
                        f"지금 가장 핫한 이 모험을 떠나보시겠어요?"
                    )
                else:
                    answer_text = "아직 등록된 공개 시나리오가 없습니다. 첫 번째 모험을 만들어보세요!"

            except Exception as e:
                logger.error(f"DB Query Error: {e}")
                answer_text = "인기 시나리오 정보를 불러오는 중 오류가 발생했습니다."

            return {
                "answer": answer_text,
                "choices": ["메인으로 이동", "게임 플레이 방법", "처음으로"]
            }

        # 7. 플레이 / 게임
        if any(w in query for w in ['플레이', '게임', '시작', 'play', '하기']):
            return {
                "answer": "🎮 **게임 플레이 방법**\n\n메인 화면에 있는 다양한 장르(판타지, 스릴러 등)의 시나리오 중 하나를 선택해 보세요.\n**'PLAY'** 버튼을 누르면 AI 게임마스터와 함께 1:1 모험이 시작됩니다.",
                "choices": ["인기 시나리오 추천", "내 시나리오 보기", "처음으로"]
            }

        # 8. 계정 관련
        if any(w in query for w in ['로그인', '계정', '가입', '아이디', 'password']):
            return {
                "answer": "🔐 **계정 관리**\n\n우측 상단의 **LOGIN** 버튼을 통해 로그인하거나 회원가입할 수 있습니다.\n구글, 카카오, 네이버 소셜 로그인도 지원합니다.",
                "choices": ["마이페이지로 이동", "처음으로"]
            }

        # 기본 응답
        return {
            "answer": f"죄송합니다. 말씀하신 '{query}'에 대한 정확한 정보를 찾지 못했습니다.\n하지만 아래 메뉴를 통해 도움을 드릴 수 있습니다.",
            "choices": ["시나리오 제작 방법", "요금제 안내", "문의하기"]
        }