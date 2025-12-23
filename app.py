import os
import logging
import json
import glob
from flask import Flask, render_template, request, render_template_string
from dotenv import load_dotenv

# --- [중요] 네가 만든 모듈들 Import ---
try:
    from builder_agent import generate_scenario_data
    from game_engine import create_game_graph
    from schemas import GameScenario
except ImportError as e:
    print(f"!!! 중요 !!! 필수 파일이 없습니다: {e}")
    print("builder_agent.py, game_engine.py, schemas.py, llm_factory.py가 같은 폴더에 있는지 확인하셈.")
    raise e

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# --- DB 경로 설정 ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FOLDER = os.path.join(BASE_DIR, 'DB')

# --- In-Memory DB ---
db = {
    "config": {
        "title": "미정",
        "dice_system": "1d20"
    },
    "state": None,
    "game_graph": None
}

# --- HTML 템플릿 조각 (Frontend Update용) ---
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
            <div class="flex justify-between border-b border-gray-800 py-1">
                <span>{{ k|upper }}</span>
                <span class="text-white font-bold">{{ v }}</span>
            </div>
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


# --- 라우트 (Routes) ---

@app.route('/')
def index():
    # [수정] 새로고침(F5) 시 상태 초기화 (메인 페이지 접속 시)
    global db
    db['state'] = None
    db['game_graph'] = None
    return render_template('index.html')


@app.route('/views/builder')
def view_builder():
    return render_template('builder_view.html')


@app.route('/views/player')
def view_player():
    p_vars = {}
    chat_log = ""

    # [수정] 탭 이동 후 돌아왔을 때 상태 복구
    if db['state']:
        p_vars = db['state'].get('player_vars', {})
        # 저장된 채팅 내역이 있으면 불러오기
        chat_log = db['state'].get('chat_log_html', "")

    return render_template('player_view.html', vars=p_vars, chat_log=chat_log)


@app.route('/views/scenes')
def view_scenes():
    # [추가] 전체 씬 목록 보기
    if not db['state'] or not db['state'].get('scenario'):
        return """
        <div class="flex-1 flex items-center justify-center h-full text-gray-500 p-8">
            <div class="text-center">
                <i data-lucide="alert-circle" class="w-12 h-12 mx-auto mb-4 opacity-50"></i>
                <h3 class="text-xl font-bold mb-2">시나리오 없음</h3>
                <p>먼저 시나리오를 생성하거나 불러와주세요.</p>
            </div>
            <script>lucide.createIcons();</script>
        </div>
        """

    scenario = db['state']['scenario']
    scenes = scenario.get('scenes', [])
    return render_template('scenes_view.html', scenes=scenes, title=scenario.get('title', 'Unknown'))


# [API] 저장된 시나리오 목록 조회
@app.route('/api/scenarios')
def list_scenarios():
    if not os.path.exists(DB_FOLDER):
        try:
            os.makedirs(DB_FOLDER)
        except OSError:
            pass

    files = []
    if os.path.exists(DB_FOLDER):
        files = [f for f in os.listdir(DB_FOLDER) if f.endswith('.json')]

    if not files:
        return '<div class="col-span-1 md:col-span-2 text-center text-gray-500 py-8 bg-gray-900/50 rounded-lg border border-gray-800">저장된 시나리오 파일이 없습니다.<br><span class="text-xs">DB 폴더를 확인하세요.</span></div>'

    html = ""
    for f in files:
        file_path = os.path.join(DB_FOLDER, f)
        title = f.replace('.json', '')
        desc = "저장된 시나리오 파일"

        try:
            with open(file_path, 'r', encoding='utf-8') as json_file:
                data = json.load(json_file)
                if 'scenario' in data:
                    s_data = data['scenario']
                    title = s_data.get('title', title)
                    if 'prologue_text' in s_data:
                        desc = s_data['prologue_text'][:60] + "..."
                else:
                    title = data.get('title', title)
                    if 'background' in data:
                        desc = data['background'][:60] + "..."
                    elif 'prologue_text' in data:
                        desc = data['prologue_text'][:60] + "..."
        except Exception as e:
            desc = f"파일 읽기 오류: {e}"

        html += f"""
        <div class="bg-gray-800 p-5 rounded-lg border border-gray-700 hover:border-indigo-500 transition-colors group flex flex-col justify-between h-full">
            <div>
                <div class="flex justify-between items-start mb-2">
                    <h4 class="font-bold text-white text-lg group-hover:text-indigo-400 line-clamp-1">{title}</h4>
                </div>
                <div class="text-xs text-gray-500 mb-2 font-mono">{f}</div>
                <p class="text-sm text-gray-400 mb-4 line-clamp-2 min-h-[2.5em]">{desc}</p>
            </div>
            <button hx-post="/api/load_scenario" 
                    hx-vals='{{"filename": "{f}"}}'
                    hx-target="#init-result"
                    class="w-full bg-indigo-900/80 hover:bg-indigo-800 text-indigo-200 py-2.5 rounded text-sm font-bold transition-colors flex items-center justify-center gap-2 border border-indigo-800/50">
                <i data-lucide="upload" class="w-4 h-4"></i>
                이 시나리오 플레이
            </button>
        </div>
        """

    html += '<script>lucide.createIcons();</script>'
    return html


# [API] 시나리오 파일 로드 (DB -> Memory)
@app.route('/api/load_scenario', methods=['POST'])
def load_scenario():
    filename = request.form.get('filename')
    if not filename:
        return '<div class="text-red-500">파일명이 없습니다.</div>'

    file_path = os.path.join(DB_FOLDER, filename)

    try:
        if not os.path.exists(file_path):
            return f'<div class="text-red-500">파일을 찾을 수 없습니다: {filename}</div>'

        with open(file_path, 'r', encoding='utf-8') as f:
            full_data = json.load(f)

        if 'scenario' in full_data:
            scenario_json = full_data['scenario']
            initial_vars = full_data.get('player_vars', scenario_json.get('initial_state', {}))
        else:
            scenario_json = full_data
            initial_vars = scenario_json.get('initial_state', {})

        db['config']['title'] = scenario_json.get('title', 'Loaded Scenario')
        db['config']['dice_system'] = scenario_json.get('dice_system', '1d20')

        if 'hp' not in initial_vars: initial_vars['hp'] = 100
        if 'max_hp' not in initial_vars: initial_vars['max_hp'] = 100

        db['state'] = {
            "scenario": scenario_json,
            "current_scene_id": "",
            "player_vars": initial_vars,
            "history": [],
            "last_user_choice_idx": -1,
            "system_message": "Scenario Loaded from DB",
            "npc_output": "",
            "narrator_output": "",
            "critic_feedback": "",
            "retry_count": 0,
            "chat_log_html": ""  # [추가] 채팅 내역 저장용
        }

        if scenario_json.get('scenes'):
            db['state']['current_scene_id'] = scenario_json['scenes'][0]['scene_id']
        else:
            db['state']['current_scene_id'] = "start"

        db['game_graph'] = create_game_graph()

        # [수정] 로드 성공 시 모달 닫기 로직 강화
        # 단순 class 제거가 아니라, 플레이어 뷰 전체를 새로고침하거나 확실하게 닫는 스크립트 전송
        success_msg = f'''
        <div class="bg-green-900/30 border border-green-800 text-green-400 p-4 rounded-lg flex items-center gap-3 fade-in mt-4">
            <i data-lucide="check-circle" class="w-6 h-6"></i>
            <div>
                <div class="font-bold">"{db["config"]["title"]}" 로드 완료!</div>
                <div class="text-sm opacity-80">채팅창에 "시작"을 입력하여 모험을 떠나세요.</div>
            </div>
        </div>
        <script>
            lucide.createIcons();
            // 모달 닫기
            (function(){{
                const modal = document.getElementById('load-modal');
                if(modal) {{
                    modal.classList.add('hidden');
                    // 혹시 몰라 강제 스타일 적용
                    modal.style.display = 'none';
                    // tailwind hidden 클래스 확실히 적용
                    setTimeout(() => modal.classList.add('hidden'), 50);
                }}
            }})();
        </script>
        '''

        # [추가] 로드 안내 메시지도 채팅 로그에 저장
        db['state']['chat_log_html'] += success_msg

        return success_msg

    except Exception as e:
        logging.error(f"로드 실패: {e}")
        import traceback
        traceback.print_exc()
        return f'<div class="text-red-500 bg-red-900/20 p-3 rounded border border-red-800">로드 실패: {str(e)}</div>'


# [API] 게임 초기화 (Builder Agent 호출)
@app.route('/api/init_game', methods=['POST'])
def init_game():
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return '<div class="text-red-500">오류: .env 파일에 OPENROUTER_API_KEY가 없습니다.</div>'

    title = request.form.get('title')
    background = request.form.get('background', 'Fantasy')
    player_hp = 20
    dice_sys = request.form.get('dice_system', '1d20')

    # 사용자가 직접 입력한 씬 요구사항 수집
    custom_scene_requirements = []
    req_keys = ['scene_req_desc', 'scene_req_npc', 'scene_req_items', 'scene_req_branch']
    for key in req_keys:
        val = request.form.get(key)
        if val and val.strip():
            custom_scene_requirements.append(val.strip())

    db['config']['title'] = title
    db['config']['dice_system'] = dice_sys

    draft_data = {
        "title": title,
        "theme": background,
        "player_hp": player_hp,
        "dice_system": dice_sys,
        "scene_guidelines": custom_scene_requirements
    }

    try:
        logging.info("Generating scenario with CrewAI...")
        scenario_json = generate_scenario_data(api_key, draft_data)
        logging.info("Scenario generated successfully.")

        # 생성된 시나리오 자동 저장
        safe_title = "".join([c for c in title if c.isalnum() or c in (' ', '-', '_')]).strip().replace(' ', '_')
        save_path = os.path.join(DB_FOLDER, f"{safe_title}.json")
        try:
            if not os.path.exists(DB_FOLDER): os.makedirs(DB_FOLDER)

            save_data = {
                "scenario": scenario_json,
                "player_vars": scenario_json.get('initial_state', {})
            }

            with open(save_path, 'w', encoding='utf-8') as f:
                json.dump(save_data, f, ensure_ascii=False, indent=2)
            logging.info(f"Scenario saved to {save_path}")
        except Exception as save_err:
            logging.error(f"File save failed: {save_err}")

        initial_vars = scenario_json.get('initial_state', {})
        if 'hp' not in initial_vars: initial_vars['hp'] = player_hp

        db['state'] = {
            "scenario": scenario_json,
            "current_scene_id": "",
            "player_vars": initial_vars,
            "history": [],
            "last_user_choice_idx": -1,
            "system_message": "Game Init",
            "npc_output": "",
            "narrator_output": "",
            "critic_feedback": "",
            "retry_count": 0,
            "chat_log_html": ""  # [추가]
        }

        if scenario_json.get('scenes'):
            db['state']['current_scene_id'] = scenario_json['scenes'][0]['scene_id']
        else:
            db['state']['current_scene_id'] = "start"

        db['game_graph'] = create_game_graph()

        return f'''
        <div class="bg-green-900/30 border border-green-800 text-green-400 p-4 rounded-lg flex items-center gap-3 fade-in">
            <i data-lucide="check-circle" class="w-6 h-6"></i>
            <div>
                <div class="font-bold">시나리오 생성 완료!</div>
                <div class="text-sm opacity-80">파일 저장됨: {safe_title}.json</div>
            </div>
        </div>
        <script>lucide.createIcons();</script>
        '''

    except Exception as e:
        logging.error(f"시나리오 생성 실패: {e}")
        import traceback
        traceback.print_exc()
        return f'<div class="text-red-500 text-xs">오류 발생: {str(e)}</div>'


# [API] 플레이어 행동 처리 (로직 통합 및 버그 수정)
@app.route('/game/act', methods=['POST'])
def game_act():
    if not db['state']:
        return "<div class='text-red-500'>게임이 로드되지 않았습니다. 상단의 '시나리오 불러오기'를 눌러주세요.</div>"

    action_text = request.form.get('action', '').strip()
    current_state = db['state']
    scenario = current_state['scenario']

    # 사용자 채팅 표시
    user_html = render_template_string(T_CHAT_MSG, sender="Player", text=action_text, is_gm=False)

    # 1. 입력값 분석 (Choice Index 계산)
    curr_scene_id = current_state['current_scene_id']
    all_scenes = {s["scene_id"]: s for s in scenario["scenes"]}
    curr_scene = all_scenes.get(curr_scene_id)

    choice_idx = -1

    # "시작" 같은 명령어는 초기화(-1)로 처리, 그 외는 선택지 찾기
    if action_text in ["시작", "start", "Start"]:
        choice_idx = -1
    elif curr_scene and curr_scene.get('choices'):
        # 숫자 입력 (1, 2, 3...)
        if action_text.isdigit():
            idx = int(action_text) - 1
            if 0 <= idx < len(curr_scene['choices']):
                choice_idx = idx
        # 텍스트 매칭
        if choice_idx == -1:
            for i, choice in enumerate(curr_scene['choices']):
                if action_text.lower() in choice['text'].lower():
                    choice_idx = i
                    break

    # 2. 상태 업데이트
    current_state['last_user_choice_idx'] = choice_idx

    try:
        if not db['game_graph']:
            db['game_graph'] = create_game_graph()

        game_app = db['game_graph']

        # 3. AI 엔진 실행
        final_state = game_app.invoke(current_state)
        db['state'] = final_state  # 결과 저장

        # 4. 결과 출력 구성
        npc_say = final_state.get('npc_output', '')
        narrator_say = final_state.get('narrator_output', '')
        sys_msg = final_state.get('system_message', '')

        full_text = ""

        # (A) 게임 시작(프롤로그)인 경우
        if "Game Started" in sys_msg or (choice_idx == -1 and "Unknown" not in sys_msg):
            prologue = scenario.get('prologue_text', '')
            if prologue:
                full_text += f"<div class='mb-4 text-gray-300 italic'>[Prologue] {prologue}</div>"

            # 첫 씬 설명 추가
            if not narrator_say:
                new_scene_id = final_state['current_scene_id']
                new_scene_obj = all_scenes.get(new_scene_id)
                if new_scene_obj:
                    full_text += f"<div class='text-xl font-bold text-indigo-300 mb-2'>{new_scene_obj.get('title', '')}</div>"
                    full_text += f"<div class='mb-4'>{new_scene_obj.get('description', '')}</div>"

        # (B) 일반 진행
        if sys_msg and "Game Started" not in sys_msg and "Game Init" not in sys_msg:
            full_text += f"<div class='text-xs text-gray-500 mb-2'>[System] {sys_msg}</div>"

        if npc_say:
            full_text += f"<div class='bg-gray-800/80 p-3 rounded-lg border-l-4 border-yellow-500 mb-4'><span class='text-yellow-400 font-bold block mb-1'>NPC</span>{npc_say}</div>"

        if narrator_say:
            full_text += narrator_say

        # 5. 다음 선택지 렌더링
        new_scene_id = final_state['current_scene_id']
        new_scene = all_scenes.get(new_scene_id)

        if new_scene and new_scene.get('choices'):
            full_text += "<div class='mt-4 pt-4 border-t border-gray-700 space-y-2'>"
            for i, c in enumerate(new_scene['choices']):
                full_text += f"""
                <button onclick="submitGameAction('{i + 1}')" class="w-full text-left bg-gray-800/50 hover:bg-indigo-900/40 p-3 rounded-lg border border-gray-700 hover:border-indigo-500 transition-all group flex items-start gap-3">
                    <span class="bg-indigo-900 text-indigo-200 text-xs font-bold px-2 py-0.5 rounded group-hover:bg-indigo-500 group-hover:text-white transition-colors">{i + 1}</span>
                    <span class="text-indigo-200 group-hover:text-white transition-colors text-sm">{c['text']}</span>
                </button>
                """
            full_text += "</div>"

        if not full_text.strip():
            full_text = f"[System] {sys_msg} (올바른 선택지를 입력해주세요)"

        gm_html = render_template_string(T_CHAT_MSG, sender="GM", text=full_text, is_gm=True)
        stats_html = render_template_string(T_STATS_OOB, vars=final_state['player_vars'])

        # [추가] 채팅 로그 서버 저장 (User MSG + GM MSG)
        if 'chat_log_html' not in db['state']:
            db['state']['chat_log_html'] = ""
        db['state']['chat_log_html'] += user_html + gm_html

        return user_html + gm_html + stats_html

    except Exception as e:
        logging.error(f"게임 턴 처리 중 오류: {e}")
        import traceback
        traceback.print_exc()
        return user_html + f"<div class='text-red-500'>시스템 오류: {e}</div>"


if __name__ == '__main__':
    app.run(debug=True, port=5000)