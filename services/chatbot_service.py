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

    # ▼▼▼ [확장됨] 키워드 분석 로직 (빌더 관련 질문 세분화) ▼▼▼
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

        # 1. 계정 관리
        if any(w in query for w in ['탈퇴', '비밀번호', '비번', 'password', '수정', '변경', '프로필', 'account']):
            return {
                "answer": "🔐 **계정 관리 안내**\n\n회원 탈퇴 및 비밀번호 수정은 **마이페이지**에서 가능합니다.\n\n1. 우측 상단 프로필 클릭 > **마이페이지** 이동\n2. 좌측 메뉴에서 **'프로필 수정'** 클릭\n3. 해당 화면에서 비밀번호 변경 및 회원 탈퇴(하단)를 하실 수 있습니다.",
                "choices": ["마이페이지로 이동", "처음으로"]
            }

        # 2. 무료 기능
        if any(w in query for w in ['무료', 'free', 'adventurer', '공짜']):
            return {
                "answer": "🎒 **Adventurer (Free) 플랜**\n\n입문자를 위한 기본 플랜입니다.\n\n✅ **주요 혜택**\n• 시나리오 생성 3개\n• 기본 AI 모델 사용\n• 커뮤니티 접근\n\n부담 없이 TRPG의 세계를 경험해보세요!",
                "choices": ["시나리오 제작 방법", "다른 요금제 보기", "처음으로"]
            }

        # 3. 요금제
        if any(w in query for w in ['요금', '가격', '비용', '결제', 'plan', '구독']):
            return {
                "answer": "💳 **요금제 안내**\n\n모험가님의 스타일에 맞는 플랜을 선택하세요!\n\n🔹 **Adventurer (Free)**: 무료, 기본 기능\n🔹 **Dungeon Master (9,900원/월)**: 무제한 생성, GPT-4, 이미지 50회\n🔹 **World Creator (29,900원/월)**: 모든 기능 + 전용 파인튜닝 모델\n\n자세한 내용은 마이페이지에서 확인 가능합니다.",
                "choices": ["마이페이지로 이동", "무료 기능 더보기", "처음으로"]
            }

        # ▼▼▼ [순서 변경] 상세 기능을 먼저 체크하도록 위로 올림 ▼▼▼

        # 5-1. 씬 추가 방법
        if any(w in query for w in ['씬', 'scene']) and any(w in query for w in ['추가', '생성', '만들']):
            return {
                "answer": "🎬 **Scene(장면) 추가 방법**\n\n캔버스 빈 곳을 우클릭하거나 상단 **'+' 버튼**을 눌러 노드를 생성할 수 있습니다.\n생성된 노드를 클릭하면 내용을 편집할 수 있습니다.",
                "choices": ["엔딩은 어떻게 만드나요?", "이미지 생성 방법", "빌더 모드 이동"]
            }

        # 5-2. 엔딩 추가
        if any(w in query for w in ['엔딩', 'ending', '결말', '끝', 'finish']):
            return {
                "answer": "🏁 **Ending(엔딩) 추가 방법**\n\n이야기의 끝을 만드는 방법은 간단합니다.\n\n1. 새로운 씬을 추가하여 결말 내용을 작성하세요.\n2. 해당 씬에서 **다른 씬으로 연결되는 선택지(Choice)를 만들지 않으면**, 자동으로 엔딩으로 처리됩니다.",
                "choices": ["씬 추가 방법", "내용 AI 작성", "빌더 모드 이동"]
            }

        # 5-3. 내용/지문 AI 작성 (Magic Write)
        if any(w in query for w in ['내용', '지문', '본문', 'text', '작성']) and any(
                w in query for w in ['ai', '자동', 'auto', '추천']):
            return {
                "answer": "✨ **AI 지문 작성 (Magic Write)**\n\n글쓰기가 막막하신가요?\n\n씬 내용 입력창 옆의 **'AI 작성(마법봉 아이콘)'**을 클릭해 보세요.\n'어두운 숲, 긴장감' 같은 키워드만 입력하면 AI가 몰입감 있는 묘사를 자동으로 작성해 줍니다.",
                "choices": ["이미지 생성 방법", "AI 제안 노트", "빌더 모드 이동"]
            }

        # 5-4. AI 제안 노트 (Brainstorming)
        if any(w in query for w in ['제안', '노트', 'note', '아이디어', '브레인', 'brain']):
            return {
                "answer": "💡 **AI 제안 노트 (Brainstorming)**\n\n다음 이야기가 떠오르지 않을 때 사용하세요!\n\n우측 패널의 **'AI Note'** 탭을 클릭하면, 현재까지의 스토리를 분석하여 AI가 **3가지 흥미로운 전개**를 제안해 줍니다. 마음에 드는 제안은 바로 적용할 수 있습니다.",
                "choices": ["내용 AI 작성", "엔딩 추가 방법", "처음으로"]
            }

        # 5-5. 씬 배경 설정
        if any(w in query for w in ['배경', 'background', 'bg']):
            return {
                "answer": "🖼️ **씬 배경(Background) 설정**\n\n씬 에디터의 **'이미지/배경'** 섹션에서 설정할 수 있습니다.\n\n1. **URL 입력**: 외부 이미지 주소를 직접 입력합니다.\n2. **AI 생성**: 씬 내용을 기반으로 AI가 배경을 그려주도록 할 수 있습니다.",
                "choices": ["이미지 생성 방법", "AI 제안 노트", "빌더 모드 이동"]
            }

        # 5-6. 이미지 생성
        if any(w in query for w in ['이미지', '그림', '삽화', 'image', 'picture']) and any(
                w in query for w in ['생성', '만들', 'gen', '그려']):
            return {
                "answer": "🎨 **AI 이미지 생성**\n\n텍스트만으로는 부족하다면 이미지를 생성해 보세요.\n\n씬 에디터 하단의 **'이미지 생성'** 버튼을 누르면, 현재 작성된 **상황 묘사와 분위기**를 AI가 분석하여 어울리는 일러스트를 즉석에서 생성해 줍니다.",
                "choices": ["씬 배경 설정", "내용 AI 작성", "빌더 모드 이동"]
            }

        # 5-7. 진입 조건 (Entry Condition)
        if any(w in query for w in ['진입', '조건', '분기', '연결']):
            return {
                "answer": "🔀 **진입 조건 (Entry Condition)**\n\n이전 씬에서의 선택지나 변수 상태에 따라 해당 씬으로의 진입 여부를 결정합니다.",
                "choices": ["씬 추가 방법", "AI 제안 노트", "처음으로"]
            }

        # 5-8. AI 자동 생성 팁 (NPC/적/아이템 공통) - [이제 여기가 먼저 실행됩니다!]
        if any(w in query for w in ['ai', '자동', 'auto']) and any(
                w in query for w in ['팁', 'tip', '요청', 'request', '어떻게', '잘']):
            return {
                "answer": "✨ **AI 자동생성 요청 꿀팁 (프롬프트 가이드)**\n\nAI에게 원하는 것을 정확히 전달하려면 **'누가/무엇이'**, **'어떤 분위기'**, **'핵심 특징'**을 포함하는 것이 좋습니다.\n\n💡 **입력 예시 (그대로 써보세요!)**\n\n1. **NPC**: \"마을 경비병, 겉으론 무뚝뚝하지만 사탕을 좋아하는 아저씨, 겁이 많음\"\n2. **적(Monster)**: \"고대 유적을 지키는 녹슨 골렘, 느리지만 강력한 한 방, 붉은 눈\"\n3. **아이템**: \"저주받은 핏빛 단검, 사용할 때마다 사용자의 체력을 흡수함\"\n\n키워드만 나열해도 AI가 찰떡같이 알아듣고 상세 설정을 채워줍니다!",
                "choices": ["NPC 생성 방법", "적 생성 방법", "아이템 생성 방법"]
            }

        # 5-9. NPC 생성 가이드
        if any(w in query for w in ['npc', '등장인물']):
            return {
                "answer": "👥 **NPC 생성 가이드**\n\nNPC 생성 탭에서 다음 정보를 입력하여 모험을 돕거나 방해하는 인물을 만듭니다.\n\n• **필수**: 이름, 역할/직업, 성격/특징, 대표 대사\n• **상세**: 외모 묘사, 배경 설정, 숨겨진 비밀\n• **설정**: 나이대, 협력도(우호/중립/비협조)\n\n생성된 NPC는 시나리오에 생동감을 더하고 플레이어와 상호작용합니다.",
                "choices": ["AI 자동 생성 팁", "적(Enemy) 생성 방법", "아이템 생성 방법"]
            }

        # 5-10. 적(Enemy) 생성 가이드
        if any(w in query for w in ['적', 'enemy', '몬스터', 'monster', '전투']):
            return {
                "answer": "⚔️ **적(Enemy) 생성 가이드**\n\n전투 탭에서 플레이어를 위협하는 적을 생성합니다.\n\n• **기본**: 이름, 종족/유형, 난이도(하~보스)\n• **스탯**: 체력(HP), 공격력(ATK)\n• **상세**: 특징/공격 패턴, 약점, 드랍 아이템\n\n생성된 적은 전투 이벤트 발생 시 등장하여 긴장감을 줍니다.",
                "choices": ["AI 자동 생성 팁", "NPC 생성 방법", "아이템 생성 방법"]
            }

        # 5-11. 아이템(Item) 생성 가이드
        if any(w in query for w in ['아이템', 'item', '보상']):
            return {
                "answer": "💎 **아이템(Item) 생성 가이드**\n\n아이템 탭에서 보상이나 중요 물품을 생성합니다.\n\n• **정보**: 이름, 유형(무기/방어구/소모품 등)\n• **상세**: 효과/능력치, 설명/외형\n\n적 처치 보상이나 이벤트 획득 아이템으로 활용됩니다.",
                "choices": ["AI 자동 생성 팁", "NPC 생성 방법", "적 생성 방법"]
            }

        # ▲▲▲ [상세 기능 체크 끝] ▲▲▲

        # 4. 시나리오 제작 (일반) - [이제 여기는 위의 상세 기능을 모두 통과한 뒤에 체크합니다]
        if any(w in query for w in ['제작', '만들기', '생성', '빌더', 'create', '노드']):
            return {
                "answer": "🛠️ **시나리오 제작 (Builder Mode)**\n\nTRPG Studio는 **노드(Node) 기반 편집기**를 제공합니다.\n코딩 없이 이야기의 흐름을 시각적으로 연결하여 나만의 모험을 만들 수 있습니다.\n\n상단의 **'Start Creation'** 버튼을 눌러 캔버스를 열어보세요!",
                "choices": ["빌더 모드 이동", "씬 추가 방법", "AI 도구가 뭔가요?"]
            }

        # 6. AI 도구 (일반)
        if any(w in query for w in ['ai', '도구', 'tool', '인공지능', '기능']):
            return {
                "answer": "🤖 **AI 보조 도구 소개**\n\nTRPG Studio는 창작자를 위한 강력한 AI 도구들을 제공합니다.\n\n1. **NPC 제네레이터**: 성격/배경 자동 생성\n2. **자동 씬 묘사**: 키워드로 지문 작성\n3. **로직 검수기**: 오류 자동 분석\n\n빌더 모드에서 이 기능들을 체험해보세요!",
                "choices": ["빌더 모드 이동", "시나리오 제작 방법", "처음으로"]
            }

        # 7. 인기/추천 시나리오 로직
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

        # 8. 플레이 / 게임
        if any(w in query for w in ['플레이', '게임', '시작', 'play', '하기']):
            return {
                "answer": "🎮 **게임 플레이 방법**\n\n메인 화면에 있는 다양한 장르(판타지, 스릴러 등)의 시나리오 중 하나를 선택해 보세요.\n**'PLAY'** 버튼을 누르면 AI 게임마스터와 함께 1:1 모험이 시작됩니다.",
                "choices": ["인기 시나리오 추천", "내 시나리오 보기", "처음으로"]
            }

        # 9. 계정 관련 (일반)
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