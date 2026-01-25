import json
import logging
from typing import List, Dict, Optional

# 필요한 모듈 임포트
try:
    from core.vector_db import get_vector_db_client
    from llm_factory import LLMFactory
except ImportError:
    pass

logger = logging.getLogger(__name__)


class ChatbotService:
    @staticmethod
    async def generate_response(user_query: str, chat_history: List[Dict] = None) -> dict:
        """
        사용자의 질문을 받아 답변을 생성합니다.
        """
        # [학습 내용] AI에게 주입할 프로젝트 지식 정보
        context_text = """
        [TRPG Studio 서비스 정보]
        1. 서비스 개요: 여울(YEOUL)은 멀티 에이전트 AI 기반의 인터랙티브 TRPG 플랫폼입니다.
        2. 시나리오 제작 (Builder Mode): 노드 기반 편집기, AI 보조 도구(NPC/지문 생성), 로직 검수 제공.
        3. 요금제: Free(3개 생성), Pro(9,900원/무제한/GPT-4), Biz(29,900원/파인튜닝).
        4. 플레이: 메인 화면 리스트 선택 -> 1:1 AI GM과 플레이.
        """

        try:
            # LLM 호출 시도
            if 'LLMFactory' in globals() and hasattr(LLMFactory, 'create_llm'):
                try:
                    # 대화 히스토리 포맷팅
                    history_text = ""
                    if chat_history:
                        recent_history = chat_history[-3:]
                        for msg in recent_history:
                            role = "User" if msg.get('role') == 'user' else "AI"
                            history_text += f"{role}: {msg.get('content')}\n"

                    system_prompt = f"""
                    당신은 TRPG Studio의 친절하고 재치 있는 AI 가이드 '여울'입니다.
                    사용자는 TRPG 게임을 만들거나 플레이하는 유저입니다.

                    [지침]
                    1. 제공된 'Context' 정보를 기반으로 정확하게 답변하세요.
                    2. 정보가 없다면 솔직히 모르겠다고 하고, 메뉴 이용을 권장하세요.
                    3. 말투는 친절하고 격려하는 톤("~해요", "~해보세요!")을 사용하세요.
                    4. 답변 끝에는 항상 사용자가 이어서 할 법한 질문 2~3개를 'choices'에 담아 주세요.

                    [이전 대화 참고]
                    {history_text}

                    반드시 아래 JSON 형식을 지켜서 응답하세요. (마크다운 없이 순수 JSON만)
                    {{
                        "answer": "답변 내용... (줄바꿈은 \\n 사용)",
                        "choices": ["선택지1", "선택지2"]
                    }}
                    """

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

    # ▼▼▼ [완전 통합] 키워드 분석 로직 (누락된 항목 추가 및 필수 키워드 강화) ▼▼▼
    @staticmethod
    def get_keyword_response(query: str) -> dict:
        """
        AI 모델 연결 불가 시, 키워드 점수(Score) 기반으로 가장 적합한 답변을 찾습니다.
        """
        query = query.lower().strip()

        # 답변 템플릿 리스트
        responses = [
            # 1. 여울 서비스 소개
            {
                "keywords": ['여울', '서비스', '소개', 'yeoul', '워터', '플랫폼', '홈페이지', '사이트', '뭐야', '무슨'],
                "required": ['여울'],
                "answer": "🌊 **여울(YEOUL) 서비스 소개**\n\n여울은 **AI 기반 인터랙티브 TRPG 플랫폼**입니다.\n\n누구나 상상 속의 모험을 **시나리오로 제작**하고,\n**AI 게임마스터**와 함께 1:1로 자유롭게 플레이할 수 있는 공간입니다.\n\n여러분의 상상력을 마음껏 펼쳐보세요!",
                "choices": ["시나리오 제작 방법", "게임 플레이 방법", "요금제 안내"]
            },
            # 2. 시나리오 제작 방법 (일반)
            {
                "keywords": ['시나리오', '제작', '만들', '생성', '빌더', 'create', '노드', '방법', 'how'],
                "required": ['제작', '만들', '생성', '빌더'], # '방법' 단독은 제외
                "answer": "🛠️ **시나리오 제작 (Builder Mode)**\n\nTRPG Studio는 **노드(Node) 기반 편집기**를 제공합니다.\n코딩 없이 이야기의 흐름을 시각적으로 연결하여 나만의 모험을 만들 수 있습니다.\n\n상단의 **'Start Creation'** 버튼을 눌러 캔버스를 열어보세요!",
                "choices": ["빌더 모드 이동", "씬 추가 방법", "AI 도구가 뭔가요?"]
            },
            # [신규] 2-1. 엔딩 만드는 법 (오매칭 방지용 별도 항목)
            {
                "keywords": ['엔딩', 'ending', '결말', '끝', 'finish', '만들'],
                "required": ['엔딩', '결말', '끝'],
                "answer": "🏁 **Ending(엔딩) 추가 방법**\n\n이야기의 끝을 만드는 방법은 간단합니다.\n\n1. 새로운 씬을 추가하여 결말 내용을 작성하세요.\n2. 해당 씬에서 **다른 씬으로 연결되는 선택지(Choice)를 만들지 않으면**, 자동으로 엔딩으로 처리됩니다.",
                "choices": ["씬 추가 방법", "내용 AI 작성", "빌더 모드 이동"]
            },
            # 3. 디버그
            {
                "keywords": ['디버그', 'debug', '버그', '시각화', 'viz', '그래프', '노드', '오류', '안', 'error'],
                "required": ['디버그', 'debug', '시각화', '버그'],
                "answer": "🛠️ **디버그(Debug) 도구 안내**\n\n플레이 화면 우측 하단의 **벌레(Bug) 아이콘**을 클릭해 보세요.\n현재 씬의 상태, 변수 값, 진행 경로를 시각적으로 확인할 수 있습니다.\n\n(그래프가 안 보인다면 PC 환경에서 새로고침을 시도해 주세요!)",
                "choices": ["플레이 방법", "문의하기"]
            },
            # 4. 프리셋 vs 시나리오 차이
            {
                "keywords": ['프리셋', '시나리오', '차이', 'vs', '비교', '다른'],
                "required": ['프리셋'],
                "answer": "⚖️ **프리셋 로드 vs 시나리오 로드 차이점**\n\n두 기능은 **'어디서'** 데이터를 가져오느냐가 다릅니다.\n\n• **프리셋 로드**: 내 컴퓨터에 저장된 **JSON 파일(구조)**을 캔버스로 불러옵니다. (로컬 파일)\n• **시나리오 로드**: 서버에 저장된 **내 프로젝트**를 편집기로 불러옵니다. (클라우드 DB)\n\n즉, 프리셋은 '단순 도면 백업', 시나리오는 '진행 중인 프로젝트 전체'라고 이해하시면 됩니다!",
                "choices": ["프리셋 저장이 뭔가요?", "시나리오 제작 방법", "빌더 모드 이동"]
            },
            # [신규] 4-1. AI 도구란?
            {
                "keywords": ['ai', '도구', 'tool', '인공지능', '기능', '뭐야', '뭔가요'],
                "required": ['ai', '도구'],
                "answer": "🤖 **AI 보조 도구 소개**\n\nTRPG Studio는 창작자를 위한 강력한 AI 도구들을 제공합니다.\n\n1. **NPC 제네레이터**: 성격/배경 자동 생성\n2. **자동 씬 묘사**: 키워드로 지문 작성 (Magic Write)\n3. **로직 검수기**: 오류 자동 분석\n\n빌더 모드에서 이 기능들을 체험해보세요!",
                "choices": ["빌더 모드 이동", "이미지 생성 방법", "처음으로"]
            },
            # [신규] 4-2. 이미지 생성 방법
            {
                "keywords": ['이미지', '그림', '삽화', 'image', 'picture', '생성', '만들', 'gen', '그려'],
                "required": ['이미지', '그림', '삽화'],
                "answer": "🎨 **AI 이미지 생성**\n\n텍스트만으로는 부족하다면 이미지를 생성해 보세요.\n\n씬 에디터 하단의 **'이미지 생성'** 버튼을 누르면, 현재 작성된 **상황 묘사와 분위기**를 AI가 분석하여 어울리는 일러스트를 즉석에서 생성해 줍니다.",
                "choices": ["씬 배경 설정", "내용 AI 작성", "빌더 모드 이동"]
            },
            # [신규] 4-3. 요금제 안내
            {
                "keywords": ['요금', '가격', '비용', '결제', 'plan', '구독', '얼마', '유료', '무료'],
                "required": ['요금', '가격', '비용', '결제', 'plan', '구독', '얼마', '유료', '무료'], # 하나라도 있으면 매칭
                "answer": "💳 **요금제 안내**\n\n모험가님의 스타일에 맞는 플랜을 선택하세요!\n\n🔹 **Adventurer (Free)**: 무료, 기본 기능, 시나리오 3개\n🔹 **Dungeon Master (9,900원/월)**: 무제한 생성, GPT-4, 이미지 50회\n🔹 **World Creator (29,900원/월)**: 모든 기능 + 전용 파인튜닝 모델\n\n자세한 내용은 마이페이지에서 확인 가능합니다.",
                "choices": ["마이페이지로 이동", "무료 기능 더보기", "처음으로"]
            },
            # 5. 도망/회피
            {
                "keywords": ['도망', 'run', 'escape', '피하', '회피', '살려'],
                "required": [],
                "answer": "🏃 **도망치기**\n\n위험한 상황인가요?\n\n**\"뒤도 돌아보지 않고 전력 질주해 도망친다\"** 또는 **\"연막탄을 뿌리고 숨는다\"** 처럼 구체적으로 입력해 보세요.\nAI가 상황과 민첩성을 고려해 성공 여부를 판정해 줄 것입니다.",
                "choices": ["전투는 어떻게 해요?", "아이템 사용법"]
            },
            # 6. 전투/공격
            {
                "keywords": ['전투', '공격', '싸움', 'attack', 'fight', '죽이', '방법'],
                "required": ['전투', '공격', '싸움'],
                "answer": "⚔️ **전투 및 공격 방법**\n\n적을 만났다면 공격 방식을 묘사하세요.\n\n예시:\n• \"들고 있는 검을 휘둘러 적을 베어버린다.\"\n• \"화염구 주문을 외워 적에게 날린다.\"\n\n플레이어의 행동 -> AI의 판정(명중 여부) -> 적의 반격 순서로 진행됩니다.",
                "choices": ["도망칠 수 있나요?", "주사위 굴리는 법"]
            },
            # 7. 주사위/판정
            {
                "keywords": ['주사위', '다이스', 'dice', '굴리', '판정', 'rule', '룰', '성공'],
                "required": [],
                "answer": "🎲 **행동 판정 안내**\n\nTRPG Studio는 **자동 판정 시스템**을 사용합니다.\n\n따로 주사위 버튼을 누를 필요 없이, **\"문을 발로 찹니다\"** 또는 **\"고블린을 검으로 찌른다\"** 같이 행동을 글로 적으세요.\nAI GM이 상황에 맞춰 자동으로 주사위를 굴리고 결과를 알려줍니다!",
                "choices": ["전투는 어떻게 해요?", "아이템 사용법", "힌트가 필요해요"]
            },
            # 8. 저장/로그
            {
                "keywords": ['저장', '세이브', 'save', '불러오기', 'load', '중단', '이전', '대화', '로그'],
                "required": [],
                "answer": "💾 **저장 및 로그 확인**\n\n1. **저장**: 모든 진행 상황은 **자동 저장(Auto Save)**됩니다. 언제든 종료해도 됩니다.\n2. **이전 대화**: 화면 내용을 위로 스크롤하면 이전 대화 내역(로그)을 확인할 수 있습니다.",
                "choices": ["내 시나리오 보기", "처음으로 돌아가기", "힌트가 필요해요"]
            },
            # 9. 힌트/막힘 ('어떻게' 때문에 오매칭 되는 것 방지: required에 '어떻게' 미포함)
            {
                "keywords": ['막혔', '힌트', 'hint', '모르겠', '뭐해', '할일', '다음'],
                "required": ['막혔', '힌트', 'hint', '모르겠', '할일'],
                "answer": "💡 **진행이 막히셨나요?**\n\n자유도가 높은 게임이라 막막할 수 있습니다. 그럴 땐 이렇게 해보세요:\n\n1. **\"주변을 자세히 살펴본다\"**라고 입력하기\n2. **\"가방에 쓸만한 게 있는지 확인한다\"**라고 입력하기\n3. **\"GM, 힌트 좀 줘\"**라고 직접 물어보기 (AI가 도와줄 거예요!)",
                "choices": ["아이템 사용법", "전투 방법", "이전 대화 보기"]
            },
            # 10. 아이템 사용
            {
                "keywords": ['아이템', 'item', '사용', '장비', '인벤'],
                "required": [],
                "answer": "🎒 **아이템 사용 방법**\n\n가지고 있는 아이템을 사용하고 싶다면 행동으로 적어주세요.\n\n예시:\n• \"가방에서 포션을 꺼내 마신다.\"\n• \"획득한 열쇠로 문을 연다.\"\n\n현재 소지품은 화면의 **Inventory(가방)** 탭에서 확인할 수 있습니다.",
                "choices": ["전투 방법", "힌트가 필요해요", "처음으로"]
            },
            # 11. 씬 추가
            {
                "keywords": ['씬', 'scene', '추가', '생성', '만들'],
                "required": ['씬', 'scene'],
                "answer": "🎬 **Scene(장면) 추가 방법**\n\n캔버스 빈 곳을 우클릭하거나 상단 **'+' 버튼**을 눌러 노드를 생성할 수 있습니다.\n생성된 노드를 클릭하면 내용을 편집할 수 있습니다.",
                "choices": ["엔딩은 어떻게 만드나요?", "이미지 생성 방법", "빌더 모드 이동"]
            },
            # 12. 인기/추천 시나리오
            {
                "keywords": ['인기', '추천', '랭킹', '순위', 'popular', 'top', '1위'],
                "type": "db_popular",
                "answer": "",
                "choices": ["메인으로 이동", "게임 플레이 방법", "처음으로"]
            },
            # 13. 인사
            {
                "keywords": ['처음', '시작', 'start', 'home', '메인', 'reset', '리셋', '안녕', '반가', 'hi'],
                "required": [],
                "answer": "안녕하세요! 모험가님. 👋\n저는 TRPG Studio의 안내를 돕는 AI 가이드 '여울'입니다.\n무엇을 도와드릴까요?",
                "choices": ["시나리오 제작 방법", "요금제 안내", "게임 플레이 방법"]
            }
        ]

        # [매칭 로직]
        best_match = None
        max_score = 0

        for item in responses:
            score = 0
            # 필수 단어 체크 (있다면)
            if item.get("required") and not any(r in query for r in item["required"]):
                continue

            # 키워드 점수 계산
            for k in item.get("keywords", []):
                if k in query:
                    score += 1

            if score > max_score:
                max_score = score
                best_match = item

        if best_match:
            # [DB 연동 룰 처리]
            if best_match.get("type") == "db_popular":
                try:
                    from models import get_db, Scenario, ScenarioLike
                    from sqlalchemy import func
                    db = next(get_db())

                    top_scenario = db.query(Scenario).filter(Scenario.is_public == True) \
                        .outerjoin(ScenarioLike, Scenario.id == ScenarioLike.scenario_id) \
                        .group_by(Scenario.id) \
                        .order_by(
                        (func.count(ScenarioLike.scenario_id) * 10 + func.coalesce(Scenario.view_count, 0)).desc()) \
                        .first()

                    if top_scenario:
                        s_data = top_scenario.data if isinstance(top_scenario.data, dict) else {}
                        inner = s_data.get('scenario', s_data)
                        title = top_scenario.title or "제목 없음"
                        desc = inner.get('prologue', inner.get('desc', '설명이 없습니다.'))
                        if len(desc) > 80: desc = desc[:80] + "..."

                        return {
                            "answer": f"🏆 **현재 인기 1위 시나리오**\n\n✨ **{title}**\n📖 {desc}\n\n지금 가장 핫한 이 모험을 떠나보시겠어요?",
                            "choices": best_match["choices"]
                        }
                    else:
                        return {
                            "answer": "아직 등록된 공개 시나리오가 없습니다. 첫 번째 모험을 만들어보세요!",
                            "choices": best_match["choices"]
                        }
                except Exception as e:
                    logger.error(f"DB Query Error: {e}")
                    return {
                        "answer": "인기 시나리오 정보를 불러오는 중 오류가 발생했습니다.",
                        "choices": ["메인으로 이동", "처음으로"]
                    }

            return {
                "answer": best_match["answer"],
                "choices": best_match["choices"]
            }

        return {
            "answer": f"죄송합니다. 말씀하신 '{query}'에 대한 정확한 정보를 찾지 못했습니다.\n하지만 아래 메뉴를 통해 도움을 드릴 수 있습니다.",
            "choices": ["시나리오 제작 방법", "요금제 안내", "문의하기"]
        }