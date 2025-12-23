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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FOLDER = os.path.join(BASE_DIR, 'DB')

db = {
    "config": {"title": "미정", "dice_system": "1d20"},
    "state": None,
    "game_graph": None
}

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
<div id="player-stats-area" hx-swap-oob="true" class="bg-[#1a1a1e] rounded-lg p-4 border border-[#2d2d35] shadow-sm mb-4 fade-in">
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


@app.route('/')
def index():
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
    if db['state']:
        p_vars = db['state'].get('player_vars', {})
        chat_log = db['state'].get('chat_log_html', "")
    return render_template('player_view.html', vars=p_vars, chat_log=chat_log)


@app.route('/views/scenes')
def view_scenes():
    if not db['state'] or not db['state'].get('scenario'):
        return '<div class="text-gray-500 text-center p-8">시나리오 없음</div>'
    scenario = db['state']['scenario']
    scenes = scenario.get('scenes', [])
    endings = scenario.get('endings', [])

    mermaid_lines = ["graph TD"]
    mermaid_lines.append("classDef default fill:#1f2937,stroke:#4b5563,stroke-width:1px,color:#e5e7eb,rx:5,ry:5;")
    mermaid_lines.append("classDef start fill:#312e81,stroke:#6366f1,stroke-width:2px,color:#ffffff,font-weight:bold;")
    mermaid_lines.append("classDef endNode fill:#831843,stroke:#ec4899,stroke-width:2px,color:#ffffff;")
    mermaid_lines.append(
        "classDef prologue fill:#0f766e,stroke:#2dd4bf,stroke-width:2px,color:#ffffff,font-weight:bold,shape:rect;")

    def clean_id(s_id):
        return "".join([c for c in str(s_id) if c.isalnum() or c == '_'])

    start_scene_id = None
    if scenes:
        if any(s['scene_id'] == 'start' for s in scenes):
            start_scene_id = 'start'
        else:
            start_scene_id = scenes[0]['scene_id']

    mermaid_lines.append('prologue["프롤로그"]:::prologue')
    if start_scene_id:
        clean_start_id = clean_id(start_scene_id)
        mermaid_lines.append(f'prologue --> {clean_start_id}')

    for scene in scenes:
        s_id = clean_id(scene['scene_id'])
        title = (scene.get('title', 'No Title')[:15] + '..') if len(scene.get('title', '')) > 15 else scene.get('title',
                                                                                                                '')
        node_class = "start" if scene['scene_id'] == start_scene_id else "default"
        mermaid_lines.append(f'{s_id}["{title}"]:::{node_class}')
        if scene.get('choices'):
            for idx, choice in enumerate(scene['choices']):
                next_raw_id = choice.get('next_scene_id')
                if next_raw_id:
                    next_id = clean_id(next_raw_id)
                    mermaid_lines.append(f'{s_id} -->|"선택지 {idx + 1}"| {next_id}')

    for idx, ending in enumerate(endings):
        e_id = clean_id(ending['ending_id'])
        mermaid_lines.append(f'{e_id}["엔딩 {idx + 1}"]:::endNode')

    mermaid_code = "\n".join(mermaid_lines)
    return render_template('scenes_view.html', scenario=scenario, scenes=scenes, title=scenario.get('title'),
                           mermaid_code=mermaid_code)


@app.route('/api/scenarios')
def list_scenarios():
    if not os.path.exists(DB_FOLDER):
        try:
            os.makedirs(DB_FOLDER)
        except:
            pass
    files = []
    if os.path.exists(DB_FOLDER):
        files = [f for f in os.listdir(DB_FOLDER) if f.endswith('.json')]
    if not files: return '<div class="text-center text-gray-500 py-8">저장된 파일 없음</div>'

    html = ""
    for f in files:
        title = f.replace('.json', '')
        desc = "저장된 시나리오"
        try:
            with open(os.path.join(DB_FOLDER, f), 'r', encoding='utf-8') as json_file:
                data = json.load(json_file)
                if 'scenario' in data:
                    title = data['scenario'].get('title', title)
                    desc = data['scenario'].get('prologue_text', '')[:60] + "..."
        except:
            pass

        html += f"""
        <div class="bg-gray-800 p-5 rounded-lg border border-gray-700 hover:border-indigo-500 transition-colors group flex flex-col justify-between h-full">
            <div>
                <h4 class="font-bold text-white text-lg mb-2">{title}</h4>
                <div class="text-xs text-gray-500 mb-2 font-mono">{f}</div>
                <p class="text-sm text-gray-400 mb-4 line-clamp-2">{desc}</p>
            </div>
            <button hx-post="/api/load_scenario" hx-vals='{{"filename": "{f}"}}' hx-target="#init-result"
                    class="w-full bg-indigo-900/80 hover:bg-indigo-800 text-indigo-200 py-2 rounded text-sm font-bold flex items-center justify-center gap-2">
                <i data-lucide="upload" class="w-4 h-4"></i> 불러오기
            </button>
        </div>
        """
    html += '<script>lucide.createIcons();</script>'
    return html


@app.route('/api/load_scenario', methods=['POST'])
def load_scenario():
    filename = request.form.get('filename')
    file_path = os.path.join(DB_FOLDER, filename)
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            full_data = json.load(f)

        scenario_json = full_data.get('scenario', full_data)
        initial_vars = full_data.get('player_vars', scenario_json.get('initial_state', {}))
        if 'hp' not in initial_vars: initial_vars['hp'] = 100

        db['config']['title'] = scenario_json.get('title', 'Loaded')

        # [수정] State 초기화
        db['state'] = {
            "scenario": scenario_json,
            "current_scene_id": scenario_json['scenes'][0]['scene_id'] if scenario_json.get('scenes') else "start",
            "player_vars": initial_vars,
            "history": [],
            "last_user_choice_idx": -1,
            "last_user_input": "",
            "parsed_intent": "",
            "system_message": "Scenario Loaded",
            "npc_output": "",
            "narrator_output": "",
            "critic_feedback": "",
            "retry_count": 0,
            "chat_log_html": ""
        }

        db['game_graph'] = create_game_graph()

        # [수정] 성공 메시지에 "게임 시작" 버튼 추가
        success_msg = f'''
        <div class="bg-green-900/30 border border-green-800 text-green-400 p-4 rounded-lg fade-in mt-4">
            <div class="flex items-center gap-3 mb-3">
                <i data-lucide="check-circle" class="w-6 h-6"></i>
                <div class="font-bold">"{db["config"]["title"]}" 로드 완료!</div>
            </div>
            <button onclick="submitGameAction('시작')" class="w-full bg-blue-600 hover:bg-blue-500 text-white font-bold py-3 px-4 rounded-lg flex items-center justify-center gap-2 transition-all hover:scale-[1.02] shadow-lg cursor-pointer">
                <i data-lucide="play" class="w-5 h-5"></i> 게임 시작 (Game Start)
            </button>
        </div>
        <script>
            lucide.createIcons();
            (function(){{
                const modal = document.getElementById('load-modal');
                if(modal) {{ modal.classList.add('hidden'); modal.style.display = 'none'; }}
            }})();
        </script>
        '''
        db['state']['chat_log_html'] += success_msg
        return success_msg
    except Exception as e:
        return f'<div class="text-red-500">로드 실패: {e}</div>'


@app.route('/api/init_game', methods=['POST'])
def init_game():
    # (기존 코드 유지 - 생략)
    return "생성 로직은 기존과 동일합니다."


@app.route('/game/act', methods=['POST'])
def game_act():
    if not db['state']: return "<div class='text-red-500'>게임 로드 필요</div>"

    action_text = request.form.get('action', '').strip()
    current_state = db['state']
    scenario = current_state['scenario']

    user_html = render_template_string(T_CHAT_MSG, sender="Player", text=action_text, is_gm=False)

    curr_scene_id = current_state['current_scene_id']
    all_scenes = {s["scene_id"]: s for s in scenario["scenes"]}
    curr_scene = all_scenes.get(curr_scene_id)

    choice_idx = -1
    if action_text in ["시작", "start", "Start"]:
        choice_idx = -1
    elif curr_scene and curr_scene.get('choices'):
        if action_text.isdigit():
            idx = int(action_text) - 1
            if 0 <= idx < len(curr_scene['choices']): choice_idx = idx
        if choice_idx == -1:
            for i, choice in enumerate(curr_scene['choices']):
                if action_text.lower() in choice['text'].lower():
                    choice_idx = i
                    break

    current_state['last_user_choice_idx'] = choice_idx
    current_state['last_user_input'] = action_text

    try:
        if not db['game_graph']: db['game_graph'] = create_game_graph()

        final_state = db['game_graph'].invoke(current_state)
        db['state'] = final_state

        npc_say = final_state.get('npc_output', '')
        narrator_say = final_state.get('narrator_output', '')
        sys_msg = final_state.get('system_message', '')

        full_text = ""

        # 프롤로그 처리
        if "Game Started" in sys_msg or (choice_idx == -1 and "Unknown" not in sys_msg):
            prologue = scenario.get('prologue_text', '')
            if prologue: full_text += f"<div class='mb-4 text-gray-300 italic p-3 bg-gray-900/50 rounded border-l-2 border-indigo-500'>[Prologue] {prologue}</div>"
            if not narrator_say and final_state['current_scene_id'] in all_scenes:
                s = all_scenes[final_state['current_scene_id']]
                full_text += f"<div class='text-xl font-bold text-indigo-300 mb-2'>{s.get('title')}</div><div class='mb-4'>{s.get('description')}</div>"

        if sys_msg and "Game Started" not in sys_msg and "Game Init" not in sys_msg:
            full_text += f"<div class='text-xs text-gray-500 mb-2'>[System] {sys_msg}</div>"

        if npc_say: full_text += f"<div class='bg-gray-800/80 p-3 rounded-lg border-l-4 border-yellow-500 mb-4'><span class='text-yellow-400 font-bold block mb-1'>NPC</span>{npc_say}</div>"

        if narrator_say: full_text += narrator_say

        # [수정] 엔딩이 아닐 때만 선택지 렌더링
        new_scene_id = final_state['current_scene_id']
        new_scene = all_scenes.get(new_scene_id)

        if new_scene and new_scene.get('choices') and "ENDING REACHED" not in narrator_say:
            full_text += "<div class='mt-4 pt-4 border-t border-gray-700 space-y-2'>"
            for i, c in enumerate(new_scene['choices']):
                full_text += f"""
                <button onclick="submitGameAction('{i + 1}')" class="w-full text-left bg-gray-800/50 hover:bg-indigo-900/40 p-3 rounded-lg border border-gray-700 hover:border-indigo-500 transition-all group flex items-start gap-3 cursor-pointer">
                    <span class="bg-indigo-900 text-indigo-200 text-xs font-bold px-2 py-0.5 rounded group-hover:bg-indigo-500 group-hover:text-white transition-colors">{i + 1}</span>
                    <span class="text-indigo-200 group-hover:text-white transition-colors text-sm">{c['text']}</span>
                </button>
                """
            full_text += "</div>"

        gm_html = render_template_string(T_CHAT_MSG, sender="GM", text=full_text, is_gm=True)
        stats_html = render_template_string(T_STATS_OOB, vars=final_state['player_vars'])

        if 'chat_log_html' not in db['state']: db['state']['chat_log_html'] = ""
        db['state']['chat_log_html'] += user_html + gm_html

        return user_html + gm_html + stats_html

    except Exception as e:
        return f"<div class='text-red-500'>Error: {e}</div>"


if __name__ == '__main__':
    app.run(debug=True, port=5000)