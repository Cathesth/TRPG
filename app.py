import os
import logging
import json
import glob
import re
from flask import Flask, render_template, request, render_template_string, jsonify
from dotenv import load_dotenv

try:
    from builder_agent import generate_scenario_from_graph
    from game_engine import create_game_graph
    from schemas import GameScenario
except ImportError as e:
    print(f"File Error: {e}")
    # raise e

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FOLDER = os.path.join(BASE_DIR, 'DB')

db = {
    "config": {"title": "미정", "dice_system": "1d20"},
    "state": None,
    "game_graph": None
}

# --- 템플릿 변수들 (변경 없음) ---
T_CHAT_MSG = """
<div class="flex gap-4 fade-in mb-4">
    <div class="w-8 h-8 rounded-lg {{ 'bg-indigo-900' if is_gm else 'bg-gray-700' }} flex items-center justify-center shrink-0">
        <i data-lucide="{{ 'bot' if is_gm else 'user' }}" class="text-white w-4 h-4"></i>
    </div>
    <div class="flex-1">
        <div class="{{ 'text-indigo-400' if is_gm else 'text-gray-400' }} text-xs font-bold mb-1">{{ sender }}</div>
        <div class="{{ 'bg-[#1a1a1e]' if is_gm else 'bg-[#202025]' }} border-gray-700 p-3 rounded-lg border text-gray-300 text-sm leading-relaxed {{ 'serif-font' if is_gm else '' }}">
            {{ text | safe }}
        </div>
    </div>
</div>
<script>lucide.createIcons();</script>
"""

T_STATS_OOB = """
<div id="player-stats-area" hx-swap-oob="true" class="bg-[#1a1a1e] rounded-lg p-4 border border-[#2d2d35] shadow-sm mb-4">
    <div class="flex justify-between items-center mb-3">
        <span class="text-xs font-bold text-gray-400 uppercase">Status</span>
        <i data-lucide="activity" class="w-3 h-3 text-red-500"></i>
    </div>
    <div class="text-xs text-gray-400 mb-2 font-mono">
        {% for k, v in vars.items() %}
            {% if k != 'inventory' %}
            <div class="flex justify-between border-b border-gray-800 py-1"><span>{{ k|upper }}</span><span class="text-white font-bold">{{ v }}</span></div>
            {% endif %}
        {% endfor %}
    </div>
    {% if vars.get('inventory') %}
    <div class="border-t border-gray-700 pt-2 mt-2">
        <div class="text-[10px] text-gray-500 mb-1">INVENTORY</div>
        <div class="flex flex-wrap gap-1">
            {% for item in vars['inventory'] %}
            <span class="bg-gray-800 px-2 py-0.5 rounded text-[10px] text-indigo-300 border border-gray-700">{{ item }}</span>
            {% endfor %}
        </div>
    </div>
    {% endif %}
</div>
<script>lucide.createIcons();</script>
"""


# --- 라우트 ---

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/views/builder')
def view_builder():
    return render_template('builder_view.html')


@app.route('/views/player')
def view_player():
    p_vars = {}
    if db['state']: p_vars = db['state'].get('player_vars', {})
    return render_template('player_view.html', vars=p_vars)


@app.route('/api/scenarios')
def list_scenarios():
    if not os.path.exists(DB_FOLDER):
        try:
            os.makedirs(DB_FOLDER)
        except OSError:
            pass

    files = [f for f in os.listdir(DB_FOLDER) if f.endswith('.json')] if os.path.exists(DB_FOLDER) else []

    if not files:
        return '<div class="col-span-1 md:col-span-2 text-center text-gray-500 py-8 bg-gray-900/50 rounded-lg border border-gray-800">저장된 시나리오가 없습니다.</div>'

    html = ""
    for f in files:
        file_path = os.path.join(DB_FOLDER, f)
        title = f.replace('.json', '')
        desc = "저장된 시나리오"
        try:
            with open(file_path, 'r', encoding='utf-8') as jf:
                data = json.load(jf)
                s_data = data.get('scenario', data)
                title = s_data.get('title', title)
                if 'prologue_text' in s_data: desc = s_data['prologue_text'][:60] + "..."
        except:
            pass

        html += f"""
        <div class="bg-gray-800 p-5 rounded-lg border border-gray-700 hover:border-indigo-500 transition-colors flex flex-col justify-between h-full">
            <div>
                <h4 class="font-bold text-white text-lg mb-2">{title}</h4>
                <div class="text-xs text-gray-500 mb-2">{f}</div>
                <p class="text-sm text-gray-400 mb-4 line-clamp-2">{desc}</p>
            </div>
            <button hx-post="/api/load_scenario" hx-vals='{{"filename": "{f}"}}' hx-target="#init-result"
                    class="w-full bg-indigo-900/80 hover:bg-indigo-800 text-indigo-200 py-2.5 rounded text-sm font-bold flex justify-center gap-2 border border-indigo-800/50">
                <i data-lucide="upload" class="w-4 h-4"></i> 플레이
            </button>
        </div>
        """
    html += '<script>lucide.createIcons();</script>'
    return html


@app.route('/api/load_scenario', methods=['POST'])
def load_scenario():
    filename = request.form.get('filename')
    if not filename: return '<div class="text-red-500">파일명 누락</div>'

    try:
        with open(os.path.join(DB_FOLDER, filename), 'r', encoding='utf-8') as f:
            full_data = json.load(f)

        scenario = full_data.get('scenario', full_data)
        initial_vars = full_data.get('player_vars', scenario.get('initial_state', {}))

        if 'hp' not in initial_vars: initial_vars['hp'] = 100
        if 'max_hp' not in initial_vars: initial_vars['max_hp'] = 100

        # 시작 씬 찾기 (없으면 scene_1, 그것도 없으면 start)
        start_id = "start"
        if scenario.get('scenes'):
            start_id = scenario['scenes'][0]['scene_id']

        db['config']['title'] = scenario.get('title', 'Loaded')
        db['config']['dice_system'] = scenario.get('dice_system', '1d20')
        db['state'] = {
            "scenario": scenario,
            "current_scene_id": start_id,
            "player_vars": initial_vars,
            "history": [],
            "last_user_choice_idx": -1,  # -1은 "아직 아무것도 안 고름" (게임 시작 전)
            "system_message": "Loaded",
            "npc_output": "",
            "narrator_output": ""
        }
        db['game_graph'] = create_game_graph()

        return f'''
        <div class="bg-green-900/30 border border-green-800 text-green-400 p-4 rounded-lg flex items-center gap-3 fade-in mt-4">
            <i data-lucide="check-circle" class="w-6 h-6"></i>
            <div>
                <div class="font-bold">로드 완료!</div>
                <div class="text-sm opacity-80">채팅창에 "시작"을 입력하세요.</div>
            </div>
        </div>
        <script>
            lucide.createIcons();
            const modal = document.getElementById('load-modal');
            if(modal) modal.classList.add('hidden');
        </script>
        '''
    except Exception as e:
        return f'<div class="text-red-500">로드 실패: {e}</div>'


@app.route('/api/init_game', methods=['POST'])
def init_game():
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return jsonify({"error": "API Key 없음"}), 400

    # [수정됨] React 빌더는 JSON 데이터를 전송함 (기존 폼 데이터 X)
    if not request.is_json:
        return jsonify({"error": "잘못된 요청 형식 (JSON 필요)"}), 400

    react_flow_data = request.get_json()

    try:
        logging.info("Generating scenario from Graph...")

        # [수정됨] scenario_crew.py의 함수 호출
        scenario_json = generate_scenario_from_graph(api_key, react_flow_data)

        title = scenario_json.get('title', 'Untitled_Scenario')
        safe_title = "".join([c for c in title if c.isalnum() or c in (' ', '-', '_')]).strip().replace(' ', '_')

        save_path = os.path.join(DB_FOLDER, f"{safe_title}.json")
        if not os.path.exists(DB_FOLDER): os.makedirs(DB_FOLDER)

        # 초기 상태 (Player Vars) 설정
        initial_vars = {
            "hp": 100,
            "max_hp": 100,
            "inventory": [],
            "gold": 0
        }

        with open(save_path, 'w', encoding='utf-8') as f:
            json.dump({
                "scenario": scenario_json,
                "player_vars": initial_vars
            }, f, ensure_ascii=False, indent=2)

        # 메모리 로드
        start_id = "start"
        if scenario_json.get('scenes'):
            start_id = scenario_json['scenes'][0]['scene_id']

        db['config']['title'] = title
        db['state'] = {
            "scenario": scenario_json,
            "current_scene_id": start_id,
            "player_vars": initial_vars,
            "history": [],
            "last_user_choice_idx": -1,
            "system_message": "Init",
            "npc_output": "",
            "narrator_output": ""
        }
        db['game_graph'] = create_game_graph()

        # React 쪽에서 알림을 띄우기 위해 JSON 리턴
        return jsonify({
            "status": "success",
            "message": f"'{title}' 생성 완료! 플레이 탭으로 이동하세요.",
            "filename": f"{safe_title}.json"
        })

    except Exception as e:
        logging.error(f"Error: {e}")
        return jsonify({"error": f"생성 오류: {str(e)}"}), 500


@app.route('/game/act', methods=['POST'])
def game_act():
    if not db['state']: return "<div class='text-red-500'>먼저 게임을 로드해주세요.</div>"

    action_text = request.form.get('action', '').strip()
    user_html = render_template_string(T_CHAT_MSG, sender="Player", text=action_text, is_gm=False)

    current_state = db['state']
    scenario = current_state['scenario']

    # 1. 현재 씬 데이터 가져오기 (리스트 루프 대신 딕셔너리로 빠르게 검색)
    all_scenes = {s["scene_id"]: s for s in scenario.get("scenes", [])}
    # 엔딩도 검색 가능하게 추가 (혹시 모르니)
    for e in scenario.get("endings", []):
        all_scenes[e["ending_id"]] = e

    curr_scene_id = current_state['current_scene_id']
    curr_scene = all_scenes.get(curr_scene_id)

    # 2. 선택지 파싱 로직 (통합됨)
    choice_idx = -1
    if curr_scene and curr_scene.get('choices'):
        # 2-1. 숫자 입력 ("1", "2")
        if action_text.isdigit():
            idx = int(action_text) - 1
            if 0 <= idx < len(curr_scene['choices']):
                choice_idx = idx

        # 2-2. 텍스트 매칭 ("1.", "1번", "문으로 들어간다" 등)
        if choice_idx == -1:
            # "1. 문을 연다" 같은 형식에서 숫자만 추출 시도
            match = re.match(r"(\d+)[.\s번]", action_text)
            if match:
                idx = int(match.group(1)) - 1
                if 0 <= idx < len(curr_scene['choices']):
                    choice_idx = idx

            # 그래도 없으면 텍스트 내용 검색
            if choice_idx == -1:
                for i, c in enumerate(curr_scene['choices']):
                    if action_text.lower() in c['text'].lower():
                        choice_idx = i
                        break

    # 3. 상태 업데이트 및 그래프 실행

    current_state['last_user_choice_idx'] = choice_idx

    def generate():
        try:
            # 1. narrator 전까지 처리 (intent_parser + rule_engine/npc_actor)
            processed_state = process_before_narrator(current_state)
            db['state'] = processed_state

            npc_say = processed_state.get('npc_output', '')
            sys_msg = processed_state.get('system_message', '')
            is_ending = processed_state.get('parsed_intent') == 'ending'
            new_scene_id = processed_state['current_scene_id']

            # 2. 시스템 메시지 (효과 적용 등)
            if sys_msg and "Game Started" not in sys_msg and "Game Init" not in sys_msg and "Game Over" not in sys_msg:
                prefix_html = f"<div class='text-xs text-gray-500 mb-2'>[System] {sys_msg}</div>"
                yield f"data: {json.dumps({'type': 'prefix', 'content': prefix_html})}\n\n"

            # 3. NPC 대화
            if npc_say:
                npc_html = f"<div class='bg-gray-800/80 p-3 rounded-lg border-l-4 border-yellow-500 mb-4'><span class='text-yellow-400 font-bold block mb-1'>NPC</span>{npc_say}</div>"
                yield f"data: {json.dumps({'type': 'prefix', 'content': npc_html})}\n\n"

            # 4. 게임 시작 시 프롤로그 스트리밍
            if is_game_start:
                # 프롤로그 헤더 - 다른 메시지들과 동일한 스타일
                prologue_header = '<div class="mb-4 p-3 bg-indigo-900/30 rounded-lg border-l-4 border-indigo-500"><div class="text-indigo-400 font-bold text-sm mb-2">[Prologue]</div><div class="text-gray-300 leading-relaxed">'
                yield f"data: {json.dumps({'type': 'prefix', 'content': prologue_header})}\n\n"

                # 프롤로그 AI 스트리밍
                for chunk in prologue_stream_generator(processed_state):
                    yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"

                # 프롤로그 닫기 - prefix로 보내서 HTML로 처리되게 함
                prologue_footer = '</div></div>'
                yield f"data: {json.dumps({'type': 'section_end', 'content': prologue_footer})}\n\n"

                # 첫 씬 타이틀
                if new_scene_id in all_scenes:
                    s = all_scenes[new_scene_id]
                    scene_title_html = f"<div class='text-lg font-bold text-indigo-300 mb-2 mt-4'>{s.get('title', '')}</div>"
                    yield f"data: {json.dumps({'type': 'prefix', 'content': scene_title_html})}\n\n"

                # 첫 씬 설명 AI 스트리밍
                for chunk in scene_stream_generator(processed_state):
                    yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"

            # 5. 엔딩 처리
            elif is_ending or new_scene_id in all_endings:
                ending = all_endings.get(new_scene_id)
                ending_title = ending.get('title', 'The End') if ending else 'The End'

                # 엔딩 헤더 HTML - 다른 메시지들과 비슷하게 빨간색 계열로 심플하게
                ending_header = f'''<div class="my-4 p-4 bg-red-900/30 rounded-lg border-l-4 border-red-500">
                    <div class="text-red-400 font-bold text-sm mb-2">🎮 ENDING REACHED</div>
                    <div class="text-xl font-bold text-red-300 mb-3">"{ending_title}"</div>
                    <div class="text-gray-300 leading-relaxed">'''
                yield f"data: {json.dumps({'type': 'ending_start', 'content': ending_header})}\n\n"

                # 엔딩 나레이션 AI 스트리밍
                for chunk in ending_stream_generator(processed_state):
                    yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"

                # 엔딩 푸터 + 새로 시작 버튼 - 심플하게
                ending_footer = '''</div>
                    <div class="mt-4 pt-3 border-t border-red-500/30 text-xs text-red-400/70">THANK YOU FOR PLAYING</div>
                </div>
                <div class="mt-4 p-4 bg-gray-800/50 rounded-lg border border-gray-700">
                    <p class="text-gray-400 mb-3 text-sm">🎮 새로운 모험을 시작하시겠습니까?</p>
                    <div class="flex gap-3 flex-wrap">
                        <a href="/" class="bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-bold py-2 px-4 rounded-lg flex items-center gap-2 transition-all">
                            <i data-lucide="home" class="w-4 h-4"></i>
                            홈으로
                        </a>
                        <a href="/views/player" onclick="window.location.href='/views/player'; window.location.reload(); return false;" class="bg-green-600 hover:bg-green-500 text-white text-sm font-bold py-2 px-4 rounded-lg flex items-center gap-2 transition-all">
                            <i data-lucide="gamepad-2" class="w-4 h-4"></i>
                            새 게임
                        </a>
                    </div>
                </div>'''
                yield f"data: {json.dumps({'type': 'ending_end', 'content': ending_footer})}\n\n"

                # 엔딩 신호 전송
                yield f"data: {json.dumps({'type': 'game_ended', 'content': True})}\n\n"

            # 6. 일반 씬 전환
            else:
                # 씬 타이틀
                if new_scene_id in all_scenes:
                    s = all_scenes[new_scene_id]
                    scene_title_html = f"<div class='text-xl font-bold text-indigo-300 mb-2'>{s.get('title', '')}</div>"
                    yield f"data: {json.dumps({'type': 'prefix', 'content': scene_title_html})}\n\n"

                # 씬 설명 AI 스트리밍
                for chunk in scene_stream_generator(processed_state):
                    yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"

            # 7. 선택지 HTML 생성 (엔딩이 아닐 때만)
            if not is_ending and new_scene_id not in all_endings:
                new_scene = all_scenes.get(new_scene_id)
                if new_scene and new_scene.get('choices'):
                    choices_html = "<div class='mt-4 pt-4 border-t border-gray-700 space-y-2'>"
                    for i, c in enumerate(new_scene['choices']):
                        choices_html += f"""
                        <button onclick="submitGameAction('{i + 1}')" class="w-full text-left bg-gray-800/50 hover:bg-indigo-900/40 p-3 rounded-lg border border-gray-700 hover:border-indigo-500 transition-all group flex items-start gap-3 cursor-pointer">
                            <span class="bg-indigo-900 text-indigo-200 text-xs font-bold px-2 py-0.5 rounded group-hover:bg-indigo-500 group-hover:text-white transition-colors">{i + 1}</span>
                            <span class="text-indigo-200 group-hover:text-white transition-colors text-sm">{c['text']}</span>
                        </button>
                        """
                    choices_html += "</div>"
                    yield f"data: {json.dumps({'type': 'choices', 'content': choices_html})}\n\n"

            # 8. 스탯 정보
            stats_data = processed_state['player_vars']
            yield f"data: {json.dumps({'type': 'stats', 'content': stats_data})}\n\n"

            # 9. 완료 신호
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
    if not db['game_graph']: db['game_graph'] = create_game_graph()

    try:
        final_state = db['game_graph'].invoke(current_state)
        db['state'] = final_state  # 상태 저장 (매우 중요)
    except Exception as e:
        logging.error(f"Game Logic Error: {e}")
        return user_html + f"<div class='text-red-500'>게임 처리 오류: {e}</div>"

    # 4. 결과 렌더링
    # 4-1. 프롤로그 처리 (게임 시작 직후에만 표시)
    prologue_html = ""
    # 시스템 메시지가 "Game Started"이고, 선택지가 -1일 때만 프롤로그 표시
    if final_state.get('system_message') == "Game Started" and choice_idx == -1:
        prologue_text = scenario.get('prologue_text', '')
        if prologue_text:
            prologue_html = f"<div class='mb-4 text-indigo-200 italic'>{prologue_text}</div>"

    # 4-2. 현재(이동한) 씬 정보 가져오기
    new_scene_id = final_state['current_scene_id']
    new_scene = all_scenes.get(new_scene_id)

    full_text = prologue_html

    # 시스템 메시지 (디버깅용, 혹은 게임 알림)
    sys_msg = final_state.get('system_message', '')
    if sys_msg and sys_msg != "Game Started":
        if "Invalid" in sys_msg:
            full_text += f"<div class='text-red-400 font-bold mb-2'>⚠ {sys_msg} (다시 선택해주세요)</div>"
        else:
            full_text += f"<div class='text-xs text-gray-500 mb-2'>[System] {sys_msg}</div>"

    # NPC 대사 (안전하게 파싱 수정됨 - SyntaxError fix)
    npc_text = final_state.get('npc_output', '')
    if npc_text:
        if ':' in npc_text:
            parts = npc_text.split(':', 1)  # 첫 번째 콜론에서만 분리
            name = parts[0].strip()
            dialogue = parts[1].replace('"', '').strip()
            full_text += f"<span class='text-yellow-400 font-bold text-lg'>\"{dialogue}\"</span><br><div class='text-xs text-yellow-600 mb-4'>{name}</div>"
        else:
            # 형식이 안 맞으면 그냥 출력 (수정됨: f-string 내 백슬래시 제거)
            clean_text = npc_text.replace('"', '').strip()
            full_text += f"<span class='text-yellow-400 font-bold text-lg'>\"{clean_text}\"</span><br><div class='text-xs text-yellow-600 mb-4'>NPC</div>"

    # 나레이션 (씬 설명 포함)
    if final_state.get('narrator_output'):
        full_text += f"<div class='leading-relaxed'>{final_state['narrator_output']}</div>"

    # 씬 정보 (제목/설명) - 이동했거나 시작일 때 보여줌
    # (매 턴 보여주는 게 TRPG스러움)
    if new_scene:
        full_text += f"<div class='mt-6 mb-2 pt-4 border-t border-gray-700/50'>"
        full_text += f"<div class='text-xl font-bold text-indigo-300 mb-2'>{new_scene.get('title', '')}</div>"
        full_text += f"<div class='text-gray-400 mb-4'>{new_scene.get('description', '')}</div>"

        # 선택지 렌더링
        if new_scene.get('choices'):
            full_text += "<div class='space-y-2'>"
            for i, c in enumerate(new_scene['choices']):
                full_text += f"""
                <button class="text-left w-full hover:bg-gray-800 p-2 rounded transition-colors text-indigo-300 hover:text-indigo-200"
                        onclick="document.querySelector('input[name=action]').value='{i + 1}'; document.querySelector('form').requestSubmit()">
                    <span class='font-bold mr-2'>{i + 1}.</span> {c['text']}
                </button>
                """
            full_text += "</div>"
        else:
            # 엔딩이거나 선택지가 없는 경우
            full_text += "<div class='text-gray-500 italic'>더 이상 선택할 수 있는 길이 없습니다.</div>"

    if not full_text.strip():
        full_text = "..."

    gm_html = render_template_string(T_CHAT_MSG, sender="GM", text=full_text, is_gm=True)
    stats_html = render_template_string(T_STATS_OOB, vars=final_state['player_vars'])

    return user_html + gm_html + stats_html


if __name__ == '__main__':
    app.run(debug=True, port=5000)