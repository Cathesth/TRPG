import logging
import json
import traceback
from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import StreamingResponse, JSONResponse

from core.state import game_state
from game_engine import scene_stream_generator, prologue_stream_generator, get_narrative_fallback_message
from routes.auth import get_current_user_optional, CurrentUser

logger = logging.getLogger(__name__)

game_router = APIRouter(prefix="/game", tags=["game"])

# 최대 재시도 횟수
MAX_RETRIES = 2


@game_router.post('/act')
async def game_act():
    """HTMX Fallback (사용 안함)"""
    return "Please use streaming mode."


@game_router.post('/act_stream')
async def game_act_stream(
    request: Request,
    action: str = Form(default=''),
    model: str = Form(default='openai/tngtech/deepseek-r1t2-chimera:free'),
    user: CurrentUser = Depends(get_current_user_optional)
):
    """스트리밍 방식 - SSE (LangGraph 기반)"""
    if not game_state.state or not game_state.game_graph:
        def error_gen():
            yield f"data: {json.dumps({'type': 'error', 'content': '먼저 게임을 로드해주세요.'})}\n\n"
        return StreamingResponse(error_gen(), media_type='text/event-stream')

    action_text = action.strip()
    current_state = game_state.state

    # 선택한 모델을 상태에 저장
    if model:
        current_state['model'] = model
        logger.info(f"🤖 Using model: {model}")

    # 1. 사용자 입력 저장
    current_state['last_user_input'] = action_text
    current_state['last_user_choice_idx'] = -1

    # 2. 게임 시작 여부 판단
    is_game_start = (
        action_text.lower() in ['시작', 'start', '게임시작'] and
        current_state.get('system_message') in ['Loaded', 'Init']
    )

    def generate():
        try:
            processed_state = current_state

            if is_game_start:
                # 게임 시작 시: 그래프 실행 없이 초기화만 수행
                start_scene_id = current_state.get('start_scene_id') or current_state.get('current_scene_id')
                logger.info(f"🎮 [GAME START] Start Scene: {start_scene_id}")
                current_state['current_scene_id'] = start_scene_id
                current_state['system_message'] = 'Game Started'
            else:
                # [개선] 상태 메시지를 유저 행동에 맞게 동기화
                scenario = current_state['scenario']
                curr_scene_id = current_state['current_scene_id']
                all_scenes = {s['scene_id']: s for s in scenario.get('scenes', [])}
                curr_scene = all_scenes.get(curr_scene_id)
                scene_type = curr_scene.get('type', 'normal') if curr_scene else 'normal'

                # 행동 분석 및 상태 메시지 생성
                status_message = ""
                action_lower = action_text.lower()

                if scene_type == 'battle':
                    # 전투 씬에서의 행동별 상태 메시지
                    investigation_keywords = ['조사', '살펴', '찾', '둘러', '관찰', '확인', '탐색', 'look', 'search', 'examine']
                    attack_keywords = ['공격', '때리', '치', '베', '찌르', '쏘', '던지', '싸우', 'attack', 'hit', 'strike', 'fight']
                    defensive_keywords = ['방어', '회피', '막', '피하', '버티', '숨', '엄폐', 'block', 'defend', 'dodge', 'hide']
                    thinking_keywords = ['생각', '방법', '전략', '약점', 'think', 'strategy']

                    if any(kw in action_lower for kw in investigation_keywords):
                        status_message = "🔍 주변을 살피는 중..."
                    elif any(kw in action_lower for kw in thinking_keywords):
                        status_message = "💭 전투 상황을 분석하는 중..."
                    elif any(kw in action_lower for kw in attack_keywords):
                        status_message = "⚔️ 전투 상황 분석 중..."
                    elif any(kw in action_lower for kw in defensive_keywords):
                        status_message = "🛡️ 방어 태세 확인 중..."
                    else:
                        status_message = "⚔️ 전투 진행 중..."
                else:
                    # 일반 씬에서의 상태 메시지
                    if any(kw in action_lower for kw in ['대화', '말', '물어', '질문', 'talk', 'speak', 'ask']):
                        status_message = "💬 대화 중..."
                    elif any(kw in action_lower for kw in ['이동', '가', '향하', 'go', 'move']):
                        status_message = "🚶 이동 중..."
                    elif any(kw in action_lower for kw in ['조사', '살펴', '확인', 'look', 'examine']):
                        status_message = "🔍 조사 중..."
                    else:
                        status_message = "⏳ 행동 처리 중..."

                # 상태 메시지 출력
                if status_message:
                    status_html = f"<div class='text-xs text-indigo-400 mb-2 border-l-2 border-indigo-500 pl-2'>{status_message}</div>"
                    yield f"data: {json.dumps({'type': 'prefix', 'content': status_html})}\n\n"

                # 일반 턴: LangGraph 실행
                logger.info(f"🎮 Action: {action_text}")
                processed_state = game_state.game_graph.invoke(current_state)
                game_state.state = processed_state

            # 결과 추출
            npc_say = processed_state.get('npc_output', '')
            sys_msg = processed_state.get('system_message', '')
            intent = processed_state.get('parsed_intent')
            is_ending = (intent == 'ending')
            
            # --- [스트리밍 응답 전송] ---

            # A. 시스템 메시지
            if sys_msg and "Game Started" not in sys_msg:
                sys_html = f"<div class='text-xs text-indigo-400 mb-2 border-l-2 border-indigo-500 pl-2'>🚀 {sys_msg}</div>"
                yield f"data: {json.dumps({'type': 'prefix', 'content': sys_html})}\n\n"

            # B. NPC 대화 (NPC 이름 표시)
            if npc_say:
                # 현재 씬에서 NPC 이름 가져오기
                scenario = processed_state['scenario']
                curr_scene_id = processed_state['current_scene_id']
                all_scenes = {s['scene_id']: s for s in scenario.get('scenes', [])}
                curr_scene = all_scenes.get(curr_scene_id)
                npc_names = curr_scene.get('npcs', []) if curr_scene else []
                npc_name = npc_names[0] if npc_names else "NPC"

                npc_html = f"""
                <div class='bg-gradient-to-r from-yellow-900/30 to-yellow-800/20 p-4 rounded-lg border-l-4 border-yellow-500 mb-4 shadow-lg'>
                    <div class='flex items-center gap-2 mb-2'>
                        <i data-lucide="message-circle" class="w-4 h-4 text-yellow-400"></i>
                        <span class='text-yellow-400 font-bold text-sm uppercase tracking-wide'>{npc_name}</span>
                    </div>
                    <div class='text-gray-200 leading-relaxed pl-6'>{npc_say}</div>
                </div>
                """
                yield f"data: {json.dumps({'type': 'prefix', 'content': npc_html})}\n\n"

            # C. 프롤로그 (게임 시작 시)
            if is_game_start:
                scenario = processed_state['scenario']
                prologue_text = scenario.get('prologue') or scenario.get('prologue_text', '')

                if prologue_text and prologue_text.strip():
                    prologue_html = '<div class="mb-6 p-4 bg-indigo-900/20 rounded-xl border border-indigo-500/30"><div class="text-indigo-400 font-bold text-sm mb-3 uppercase tracking-wider">[ Prologue ]</div><div class="text-gray-200 leading-relaxed serif-font text-lg">'
                    yield f"data: {json.dumps({'type': 'prefix', 'content': prologue_html})}\n\n"

                    for chunk in prologue_stream_generator(processed_state):
                        yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"

                    yield f"data: {json.dumps({'type': 'section_end', 'content': '</div></div>'})}\n\n"
                    hr_content = '<hr class="border-gray-800 my-6">'
                    yield f"data: {json.dumps({'type': 'prefix', 'content': hr_content})}\n\n"

                # 프롤로그 후 첫 씬으로 이동
                prologue_connects_to = scenario.get('prologue_connects_to', [])
                if prologue_connects_to and len(prologue_connects_to) > 0:
                    first_scene_id = prologue_connects_to[0]
                else:
                    scenes = scenario.get('scenes', [])
                    first_scene_id = scenes[0]['scene_id'] if scenes else 'start'

                processed_state['current_scene_id'] = first_scene_id
                game_state.state = processed_state
                logger.info(f"🎮 [PROLOGUE -> SCENE] Moving to: {first_scene_id}")

                # 첫 씬 묘사 (재시도 로직 포함)
                for result in stream_scene_with_retry(processed_state):
                    yield result

            # D. 엔딩
            elif is_ending:
                ending_html = processed_state.get('narrator_output', '')
                yield f"data: {json.dumps({'type': 'ending_start', 'content': ending_html})}\n\n"
                yield f"data: {json.dumps({'type': 'game_ended', 'content': True})}\n\n"

            # E. 일반 씬 진행 (나레이션) - 재시도 로직 포함
            else:
                for result in stream_scene_with_retry(processed_state):
                    yield result

            # F. 스탯 업데이트 및 완료
            stats_data = processed_state.get('player_vars', {})
            yield f"data: {json.dumps({'type': 'stats', 'content': stats_data})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as e:
            logger.error(f"Stream Error: {e}", exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )


def stream_scene_with_retry(state):
    """씬 스트리밍 with 재시도 로직"""
    retry_count = 0

    while retry_count <= MAX_RETRIES:
        buffer = ""
        need_retry = False

        for chunk in scene_stream_generator(state, retry_count=retry_count, max_retries=MAX_RETRIES):
            # 재시도 신호 감지
            if "__RETRY_SIGNAL__" in chunk:
                need_retry = True
                break

            buffer += chunk
            yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"

        if need_retry:
            retry_count += 1
            if retry_count <= MAX_RETRIES:
                logger.info(f"🔄 [RETRY] Attempt {retry_count}/{MAX_RETRIES}")
                yield f"data: {json.dumps({'type': 'retry', 'attempt': retry_count, 'max': MAX_RETRIES})}\n\n"
            else:
                logger.warning(f"⚠️ [FALLBACK] Max retries exceeded")
                fallback_msg = get_narrative_fallback_message(state.get('scenario', {}))
                fallback_html = f"""
                <div class="bg-yellow-900/30 border border-yellow-700/50 rounded-lg p-4 my-2">
                    <div class="text-yellow-400 serif-font">{fallback_msg}</div>
                </div>
                """
                yield f"data: {json.dumps({'type': 'fallback', 'content': fallback_html})}\n\n"
                break
        else:
            # 성공적으로 완료
            break
