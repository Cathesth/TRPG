"""
게임 플레이 라우트
"""
import json
import logging
from flask import Blueprint, request, Response, stream_with_context

from core.state import game_state
from game_engine import (
    process_before_narrator,
    prologue_stream_generator,
    scene_stream_generator
)

logger = logging.getLogger(__name__)

game_bp = Blueprint('game', __name__, url_prefix='/game')


@game_bp.route('/act', methods=['POST'])
def game_act():
    """HTMX Fallback (사용 안함)"""
    return "Please use streaming mode."


@game_bp.route('/act_stream', methods=['POST'])
def game_act_stream():
    """스트리밍 방식 - SSE"""
    if not game_state.state:
        return Response(
            "data: " + json.dumps({'type': 'error', 'content': '먼저 게임을 로드해주세요.'}) + "\n\n",
            mimetype='text/event-stream'
        )

    action_text = request.form.get('action', '').strip()
    current_state = game_state.state

    # 사용자 입력 저장
    current_state['last_user_input'] = action_text
    current_state['last_user_choice_idx'] = -1

    # 게임 시작 여부 판단
    is_game_start = (
        action_text.lower() in ['시작', 'start', '게임시작'] and
        current_state.get('system_message') in ['Loaded', 'Init']
    )

    def generate():
        try:
            # 1. AI 로직 처리 (게임 시작이 아닌 경우)
            if not is_game_start:
                processed_state = process_before_narrator(current_state)
                game_state.state = processed_state
            else:
                # 게임 시작 시에는 AI 로직 없이 바로 프롤로그와 첫 씬 표시
                start_scene_id = current_state.get('start_scene_id') or current_state.get('current_scene_id')
                logger.info(f"🎮 [GAME START] Setting current_scene_id to: {start_scene_id}")
                current_state['current_scene_id'] = start_scene_id
                current_state['system_message'] = 'Game Started'
                processed_state = current_state
                game_state.state = processed_state

            npc_say = processed_state.get('npc_output', '')
            sys_msg = processed_state.get('system_message', '')
            is_ending = processed_state.get('parsed_intent') == 'ending'
            new_scene_id = processed_state['current_scene_id']

            logger.info(f"📍 [CURRENT SCENE] After processing: {new_scene_id}")

            # 2. 시스템 메시지 전송
            if sys_msg and "Game Started" not in sys_msg:
                sys_html = f"<div class='text-xs text-indigo-400 mb-2 border-l-2 border-indigo-500 pl-2'>🚀 {sys_msg}</div>"
                yield f"data: {json.dumps({'type': 'prefix', 'content': sys_html})}\n\n"

            # 3. NPC 대화 전송
            if npc_say:
                npc_html = f"<div class='bg-gray-800/80 p-3 rounded-lg border-l-4 border-yellow-500 mb-4'><span class='text-yellow-400 font-bold block mb-1'>NPC</span>{npc_say}</div>"
                yield f"data: {json.dumps({'type': 'prefix', 'content': npc_html})}\n\n"

            # 4. 프롤로그 (게임 시작 시)
            if is_game_start:
                prologue_html = '<div class="mb-6 p-4 bg-indigo-900/20 rounded-xl border border-indigo-500/30"><div class="text-indigo-400 font-bold text-sm mb-3 uppercase tracking-wider">[ Prologue ]</div><div class="text-gray-200 leading-relaxed font-serif italic text-lg">'
                yield f"data: {json.dumps({'type': 'prefix', 'content': prologue_html})}\n\n"

                # 프롤로그 출력
                for chunk in prologue_stream_generator(processed_state):
                    yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"

                prologue_footer = '</div></div>'
                yield f"data: {json.dumps({'type': 'section_end', 'content': prologue_footer})}\n\n"

                # 프롤로그 직후 첫 씬 설명
                hr_html = '<hr class="border-gray-800 my-6">'
                yield f"data: {json.dumps({'type': 'prefix', 'content': hr_html})}\n\n"

                for chunk in scene_stream_generator(processed_state):
                    yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"

            # 5. 엔딩
            elif is_ending:
                ending_html = processed_state.get('narrator_output', '')
                yield f"data: {json.dumps({'type': 'ending_start', 'content': ending_html})}\n\n"
                yield f"data: {json.dumps({'type': 'game_ended', 'content': True})}\n\n"

            # 6. 일반 씬 진행
            else:
                for chunk in scene_stream_generator(processed_state):
                    yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"

            # 7. 스탯 업데이트
            stats_data = processed_state['player_vars']
            yield f"data: {json.dumps({'type': 'stats', 'content': stats_data})}\n\n"

            # 8. 완료
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as e:
            import traceback
            traceback.print_exc()
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no'
        }
    )
