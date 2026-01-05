import logging
import json
import traceback
from flask import Blueprint, request, Response, stream_with_context, jsonify
from flask_login import login_required, current_user

from core.state import game_state
# [CRITICAL] process_before_narrator 제거, scene_stream_generator 등만 import
from game_engine import scene_stream_generator, prologue_stream_generator

# 로깅 설정
logger = logging.getLogger(__name__)

game_bp = Blueprint('game', __name__, url_prefix='/game')

@game_bp.route('/act', methods=['POST'])
def game_act():
    """HTMX Fallback (사용 안함)"""
    return "Please use streaming mode."

@game_bp.route('/act_stream', methods=['POST'])
def game_act_stream():
    """스트리밍 방식 - SSE (LangGraph 기반)"""
    if not game_state.state or not game_state.game_graph:
        return Response(
            "data: " + json.dumps({'type': 'error', 'content': '먼저 게임을 로드해주세요.'}) + "\n\n",
            mimetype='text/event-stream'
        )

    action_text = request.form.get('action', '').strip()
    current_state = game_state.state

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
                # 시작 시점에는 그래프를 돌리지 않음 (프롤로그 출력)
            else:
                # 일반 턴: LangGraph 실행
                logger.info(f"🎮 Action: {action_text}")
                # invoke를 통해 상태 갱신
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

            # B. NPC 대화
            if npc_say:
                npc_html = f"<div class='bg-gray-800/80 p-3 rounded-lg border-l-4 border-yellow-500 mb-4'><span class='text-yellow-400 font-bold block mb-1'>NPC</span>{npc_say}</div>"
                yield f"data: {json.dumps({'type': 'prefix', 'content': npc_html})}\n\n"

            # C. 프롤로그 (게임 시작 시)
            if is_game_start:
                prologue_html = '<div class="mb-6 p-4 bg-indigo-900/20 rounded-xl border border-indigo-500/30"><div class="text-indigo-400 font-bold text-sm mb-3 uppercase tracking-wider">[ Prologue ]</div><div class="text-gray-200 leading-relaxed font-serif italic text-lg">'
                yield f"data: {json.dumps({'type': 'prefix', 'content': prologue_html})}\n\n"

                for chunk in prologue_stream_generator(processed_state):
                    yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"

                yield f"data: {json.dumps({'type': 'section_end', 'content': '</div></div>'})}\n\n"
                
                # 프롤로그 후 첫 씬 구분선
                yield f"data: {json.dumps({'type': 'prefix', 'content': '<hr class=\"border-gray-800 my-6\">'})}\n\n"
                
                # 첫 씬 묘사
                for chunk in scene_stream_generator(processed_state):
                    yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"

            # D. 엔딩
            elif is_ending:
                ending_html = processed_state.get('narrator_output', '')
                yield f"data: {json.dumps({'type': 'ending_start', 'content': ending_html})}\n\n"
                yield f"data: {json.dumps({'type': 'game_ended', 'content': True})}\n\n"

            # E. 일반 씬 진행 (나레이션)
            else:
                for chunk in scene_stream_generator(processed_state):
                    yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"

            # F. 스탯 업데이트 및 완료
            stats_data = processed_state.get('player_vars', {})
            yield f"data: {json.dumps({'type': 'stats', 'content': stats_data})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as e:
            logger.error(f"Stream Error: {e}", exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )