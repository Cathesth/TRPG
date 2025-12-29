import os
import logging
import json
import glob
import re
from flask import Flask, render_template, request, render_template_string, jsonify, Response, stream_with_context
from dotenv import load_dotenv

try:
    from builder_agent import generate_scenario_from_graph
    from game_engine import (
        create_game_graph,
        process_before_narrator,
        prologue_stream_generator,
        scene_stream_generator,
        ending_stream_generator
    )
    from schemas import GameScenario
except ImportError as e:
    print(f"File Error: {e}")
    # raise e

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@app.after_request
def add_header(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, post-check=0, pre-check=0, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '-1'
    return response


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FOLDER = os.path.join(BASE_DIR, 'DB')

db = {
    "config": {"title": "미정", "dice_system": "1d20"},
    "state": None,
    "game_graph": None
}


# --- [Core] 데이터 파싱 헬퍼 (오류 해결 핵심) ---
def parse_request_data(req):
    """
    AttributeError: 'str' object has no attribute 'get' 해결을 위한 파싱 함수
    JSON 데이터가 문자열로 이중 인코딩되어 들어오거나, Content-Type 헤더 문제로 파싱 안 될 때를 대비함.
    """
    try:
        # 1. 기본 json 파싱 시도 (force=True로 헤더 무시하고 시도)
        data = req.get_json(force=True, silent=True)

        # 2. 만약 data가 None이거나(파싱실패) 문자열이면(이중인코딩) 추가 처리
        if data is None:
            # req.data가 bytes일 수 있으므로 디코딩
            data = req.data.decode('utf-8')

        if isinstance(data, str):
            # 빈 문자열이면 빈 딕셔너리 반환
            if not data.strip():
                return {}
            try:
                # 문자열로 된 JSON일 경우 다시 파싱
                data = json.loads(data)
            except json.JSONDecodeError:
                # 진짜 그냥 문자열인 경우.. 로깅 후 빈 딕셔너리 리턴 (get 호출 방지)
                logger.warning(f"JSON 파싱 실패, 원본 데이터: {data[:100]}...")
                return {}

        # 최종적으로 dict인지 확인
        return data if isinstance(data, dict) else {}

    except Exception as e:
        logger.error(f"데이터 파싱 중 치명적 오류: {e}")
        return {}


# --- 라우트 ---

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/clear_state', methods=['POST'])
def clear_state():
    """새로고침 시 state 초기화"""
    db['state'] = None
    db['game_graph'] = None
    return jsonify({"status": "cleared"})


@app.route('/views/builder')
def view_builder():
    return render_template('builder_view.html')


@app.route('/views/player')
def view_player():
    p_vars = {}
    if db['state']: p_vars = db['state'].get('player_vars', {})
    return render_template('player_view.html', vars=p_vars)


@app.route('/views/scenes')
def view_scenes():
    if not db['state']:
        return render_template('scenes_view.html',
                               title="시나리오 없음",
                               scenario={"endings": [], "prologue_text": ""},
                               scenes=[],
                               mermaid_code="graph TD\n    A[시나리오를 먼저 로드하세요]")

    scenario = db['state']['scenario']
    scenes = scenario.get('scenes', [])
    endings = scenario.get('endings', [])
    title = scenario.get('title', 'Untitled')

    mermaid_lines = ["graph TD"]
    prologue_text = scenario.get('prologue', scenario.get('prologue_text', ''))
    if prologue_text:
        mermaid_lines.append(f'    PROLOGUE["📖 Prologue"]:::prologueStyle')
        if scenes:
            mermaid_lines.append(f'    PROLOGUE --> {scenes[0]["scene_id"]}')

    for scene in scenes:
        scene_id = scene['scene_id']
        scene_title = scene.get('title', scene_id).replace('"', "'")
        mermaid_lines.append(f'    {scene_id}["{scene_title}"]:::sceneStyle')

        # Transitions 시각화
        for i, trans in enumerate(scene.get('transitions', [])):
            next_id = trans.get('target_scene_id')
            trigger = trans.get('trigger', 'action').replace('"', "'")
            if next_id:
                mermaid_lines.append(f'    {scene_id} -->|"{trigger}"| {next_id}')

    for i, ending in enumerate(endings):
        ending_id = ending['ending_id']
        ending_label = f"엔딩{i + 1}"
        mermaid_lines.append(f'    {ending_id}["🏁 {ending_label}"]:::endingStyle')

    mermaid_lines.append("    classDef prologueStyle fill:#0f766e,stroke:#14b8a6,color:#fff")
    mermaid_lines.append("    classDef sceneStyle fill:#312e81,stroke:#6366f1,color:#fff")
    mermaid_lines.append("    classDef endingStyle fill:#831843,stroke:#ec4899,color:#fff")

    mermaid_code = "\n".join(mermaid_lines)

    return render_template('scenes_view.html',
                           title=title,
                           scenario=scenario,
                           scenes=scenes,
                           mermaid_code=mermaid_code)


@app.route('/api/scenarios')
def list_scenarios():
    if not os.path.exists(DB_FOLDER):
        try:
            os.makedirs(DB_FOLDER)
        except OSError:
            pass

    files = [f for f in os.listdir(DB_FOLDER) if f.endswith('.json')]
    if not files:
        return '<div class="col-span-1 md:col-span-2 text-center text-gray-500 py-8">저장된 시나리오가 없습니다.</div>'

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
                p_text = s_data.get('prologue', s_data.get('prologue_text', ''))
                if p_text: desc = p_text[:60] + "..."
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
        # player_vars 로드 (없으면 scenario의 initial_state 사용)
        initial_vars = full_data.get('player_vars', scenario.get('initial_state', {}))

        # global variables에 정의된 초기값 병합
        # [수정] string indices must be integers 오류 방어 (g_var가 dict가 아닌 경우 처리)
        raw_vars = scenario.get('variables', [])
        if isinstance(raw_vars, list):
            for g_var in raw_vars:
                # 1. 딕셔너리인 경우 (정상)
                if isinstance(g_var, dict):
                    v_name = g_var.get('name')
                    if v_name and v_name not in initial_vars:
                        initial_vars[v_name] = g_var.get('initial_value', 0)
                # 2. 문자열인 경우 (이름만 있는 경우) -> 기본값 0 할당
                elif isinstance(g_var, str):
                    if g_var not in initial_vars:
                        initial_vars[g_var] = 0

        if 'hp' not in initial_vars: initial_vars['hp'] = 100
        if 'inventory' not in initial_vars: initial_vars['inventory'] = []

        start_id = "start"
        if scenario.get('scenes'):
            start_id = scenario['scenes'][0]['scene_id']

        db['config']['title'] = scenario.get('title', 'Loaded')
        db['state'] = {
            "scenario": scenario,
            "current_scene_id": start_id,
            "player_vars": initial_vars,
            "history": [],
            "last_user_choice_idx": -1,
            "system_message": "Loaded",
            "npc_output": "",
            "narrator_output": ""
        }
        db['game_graph'] = create_game_graph()

        return f'''
        <div class="bg-green-900/30 border border-green-800 text-green-400 p-4 rounded-lg flex items-center gap-3 fade-in mt-4">
            <i data-lucide="check-circle" class="w-6 h-6"></i>
            <div>
                <div class="font-bold">"{db['config']['title']}" 로드 완료!</div>
                <div class="text-sm opacity-80">채팅창에 "시작"을 입력하여 모험을 시작하세요.</div>
            </div>
        </div>
        <button onclick="submitGameAction('시작')" 
                class="mt-3 w-full bg-indigo-600 hover:bg-indigo-500 text-white py-3 rounded-lg font-bold flex items-center justify-center gap-2 transition-all hover:scale-[1.02] shadow-lg">
            <i data-lucide="play" class="w-5 h-5"></i>
            게임 시작하기
        </button>
        <script>
            lucide.createIcons();
            const modal = document.getElementById('load-modal');
            if(modal) modal.classList.add('hidden');
        </script>
        '''
    except Exception as e:
        logger.error(f"Load Error: {e}", exc_info=True)
        return f'<div class="text-red-500">로드 실패: {e}</div>'


@app.route('/api/init_game', methods=['POST'])
def init_game():
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key: return jsonify({"error": "API Key 없음"}), 400

    # 1. 안전한 데이터 파싱
    react_flow_data = parse_request_data(request)
    if not react_flow_data:
        return jsonify({"error": "유효하지 않은 데이터 형식"}), 400

    try:
        logging.info("Generating scenario from Graph...")

        scenario_json = generate_scenario_from_graph(api_key, react_flow_data)

        # [안전장치 1] builder_agent가 문자열을 리턴했을 경우 대비
        if isinstance(scenario_json, str):
            logging.warning(f"⚠️ Warning: scenario_json is string. Parsing... (Preview: {scenario_json[:50]})")
            try:
                scenario_json = json.loads(scenario_json)
            except Exception as parse_error:
                logging.error(f"❌ Critical: Failed to parse scenario_json string: {parse_error}")
                return jsonify({"error": "생성된 데이터 형식이 잘못되었습니다."}), 500

        # [안전장치 2] 딕셔너리가 아닌 경우 방어
        if not isinstance(scenario_json, dict):
            logging.error(f"❌ Critical: scenario_json is {type(scenario_json)}, expected dict.")
            return jsonify({"error": "생성된 데이터가 딕셔너리가 아닙니다."}), 500

        title = scenario_json.get('title', 'Untitled_Scenario')
        safe_title = "".join([c for c in title if c.isalnum() or c in (' ', '-', '_')]).strip().replace(' ', '_')

        save_path = os.path.join(DB_FOLDER, f"{safe_title}.json")
        if not os.path.exists(DB_FOLDER): os.makedirs(DB_FOLDER)

        initial_vars = {}
        # [안전장치 3] variables 루프 방어
        variables = scenario_json.get('variables', [])
        if isinstance(variables, list):
            for v in variables:
                if isinstance(v, dict):
                    initial_vars[v.get('name', 'unknown')] = v.get('initial_value', 0)
                else:
                    logging.warning(f"⚠️ Skipped invalid variable: {v}")

        # 기본값 보장
        if 'hp' not in initial_vars: initial_vars['hp'] = 100
        if 'inventory' not in initial_vars: initial_vars['inventory'] = []

        with open(save_path, 'w', encoding='utf-8') as f:
            json.dump({
                "scenario": scenario_json,
                "player_vars": initial_vars
            }, f, ensure_ascii=False, indent=2)

        start_id = "start"
        scenes = scenario_json.get('scenes', [])
        if scenes and isinstance(scenes, list) and len(scenes) > 0:
            start_id = scenes[0].get('scene_id', 'start')

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

        return jsonify({
            "status": "success",
            "message": f"'{title}' 생성 완료! 플레이 탭으로 이동하세요.",
            "filename": f"{safe_title}.json"
        })

    except Exception as e:
        logging.error(f"Error in init_game: {e}", exc_info=True)
        return jsonify({"error": f"생성 오류: {str(e)}"}), 500


@app.route('/game/act', methods=['POST'])
def game_act():
    """HTMX Fallback (사용 안함)"""
    return "Please use streaming mode."


@app.route('/game/act_stream', methods=['POST'])
def game_act_stream():
    """스트리밍 방식 - SSE"""
    if not db['state']:
        return Response("data: " + json.dumps({'type': 'error', 'content': '먼저 게임을 로드해주세요.'}) + "\n\n",
                        mimetype='text/event-stream')

    action_text = request.form.get('action', '').strip()
    current_state = db['state']

    # 사용자 입력 저장
    current_state['last_user_input'] = action_text
    current_state['last_user_choice_idx'] = -1

    # 게임 시작 여부 판단
    is_game_start = (action_text.lower() in ['시작', 'start', '게임시작'] and
                     current_state.get('system_message') in ['Loaded', 'Init'])

    def generate():
        try:
            # 1. AI 로직 처리 (game_engine 호출)
            processed_state = process_before_narrator(current_state)
            db['state'] = processed_state

            npc_say = processed_state.get('npc_output', '')
            sys_msg = processed_state.get('system_message', '')
            is_ending = processed_state.get('parsed_intent') == 'ending'
            new_scene_id = processed_state['current_scene_id']

            # 2. 시스템 메시지 전송
            if sys_msg and "Game Started" not in sys_msg:
                # f-string backslash fix
                sys_html = f"<div class='text-xs text-indigo-400 mb-2 border-l-2 border-indigo-500 pl-2'>🚀 {sys_msg}</div>"
                yield f"data: {json.dumps({'type': 'prefix', 'content': sys_html})}\n\n"

            # 3. NPC 대화 전송
            if npc_say:
                # f-string backslash fix
                npc_html = f"<div class='bg-gray-800/80 p-3 rounded-lg border-l-4 border-yellow-500 mb-4'><span class='text-yellow-400 font-bold block mb-1'>NPC</span>{npc_say}</div>"
                yield f"data: {json.dumps({'type': 'prefix', 'content': npc_html})}\n\n"

            # 4. 프롤로그 (게임 시작 시)
            if is_game_start:
                # f-string backslash fix
                prologue_html = '<div class="mb-6 p-4 bg-indigo-900/20 rounded-xl border border-indigo-500/30"><div class="text-indigo-400 font-bold text-sm mb-3 uppercase tracking-wider">[ Prologue ]</div><div class="text-gray-200 leading-relaxed font-serif italic text-lg">'
                yield f"data: {json.dumps({'type': 'prefix', 'content': prologue_html})}\n\n"

                # 프롤로그 원본 그대로 출력
                for chunk in prologue_stream_generator(processed_state):
                    yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"

                prologue_footer = '</div></div>'
                yield f"data: {json.dumps({'type': 'section_end', 'content': prologue_footer})}\n\n"

                # 프롤로그 직후 첫 씬 설명 시작
                # f-string backslash fix
                hr_html = '<hr class="border-gray-800 my-6">'
                yield f"data: {json.dumps({'type': 'prefix', 'content': hr_html})}\n\n"

                # 씬 전환
                for chunk in scene_stream_generator(processed_state):
                    yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"

            # 5. 엔딩
            elif is_ending:
                ending_html = processed_state.get('narrator_output', '')  # 이미 rule_node에서 생성됨
                yield f"data: {json.dumps({'type': 'ending_start', 'content': ending_html})}\n\n"
                yield f"data: {json.dumps({'type': 'game_ended', 'content': True})}\n\n"

            # 6. 일반 씬 진행
            else:
                for chunk in scene_stream_generator(processed_state):
                    yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"

            # 7. 선택지 대신 힌트 (트랜지션이 존재하면 힌트 제공)
            scenario = processed_state['scenario']
            all_scenes = {s['scene_id']: s for s in scenario['scenes']}
            curr_scene = all_scenes.get(new_scene_id)

            # 버튼 대신 가능한 행동 힌트 (옵션)
            if not is_ending:
                pass

                # 8. 스탯 업데이트
            stats_data = processed_state['player_vars']
            yield f"data: {json.dumps({'type': 'stats', 'content': stats_data})}\n\n"

            # 9. 완료
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


if __name__ == '__main__':
    app.run(debug=True, use_reloader=False, port=5001)