import os
import json
import logging
import time
import threading
from flask import Blueprint, request, jsonify, Response, stream_with_context
from flask_login import login_user, logout_user, login_required, current_user, UserMixin

# builder_agent에서 필요한 함수들 임포트
from builder_agent import (
    generate_scenario_from_graph,
    set_progress_callback,
    generate_single_npc
)

from core.state import game_state
from core.utils import parse_request_data, pick_start_scene_id
from services.scenario_service import ScenarioService
from services.user_service import UserService
from services.preset_service import PresetService  # 중복 제거 및 유지
from game_engine import create_game_graph

logger = logging.getLogger(__name__)

api_bp = Blueprint('api', __name__, url_prefix='/api')

# [추가] Flask-Login용 임시 User 클래스 (DB 없이 작동시키기 위함)
class User(UserMixin):
    def __init__(self, id):
        self.id = id


# --- [인증 API] ---
@api_bp.route('/auth/register', methods=['POST'])
def register():
    data = parse_request_data(request)
    username = data.get('username')
    password = data.get('password')
    email = data.get('email')  # 이메일 추가
    if not username or not password: return jsonify({"success": False, "error": "입력값 부족"}), 400
    if UserService.create_user(username, password, email): return jsonify({"success": True})
    return jsonify({"success": False, "error": "이미 존재하는 아이디"}), 400


@api_bp.route('/auth/login', methods=['POST'])
def login():
    data = parse_request_data(request)
    username = data.get('username')
    password = data.get('password')

    if not username or not password:
        return jsonify({"success": False, "error": "입력값 부족"}), 400

    # DB에서 사용자 조회
    user = UserService.verify_user(username, password)
    if user:
        login_user(user)  # Flask-Login 세션 생성
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "아이디 또는 비밀번호가 잘못되었습니다."}), 401


@api_bp.route('/auth/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return jsonify({"success": True})


@api_bp.route('/auth/me', methods=['GET'])
def get_current_user():
    return jsonify({"is_logged_in": current_user.is_authenticated,
                    "username": current_user.id if current_user.is_authenticated else None})


# --- [빌드 진행률] ---
build_progress = {"status": "idle", "progress": 0}
build_lock = threading.Lock()


def update_build_progress(**kwargs):
    global build_progress
    with build_lock:
        build_progress.update(kwargs)


@api_bp.route('/build_progress')
def get_build_progress_sse():
    def generate():
        last_data = None
        while True:
            with build_lock:
                current_data = json.dumps(build_progress)
            if current_data != last_data:
                yield f"data: {current_data}\n\n"
                last_data = current_data
                if build_progress["status"] in ["completed", "error"]: break
            time.sleep(0.3)

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


# --- [시나리오 관리] ---

@api_bp.route('/scenarios')
def list_scenarios():
    sort_order = request.args.get('sort', 'newest')
    filter_mode = request.args.get('filter', 'public')
    limit = request.args.get('limit', type=int)

    user_id = current_user.id if current_user.is_authenticated else None
    file_infos = ScenarioService.list_scenarios(sort_order, user_id, filter_mode, limit)

    if not file_infos:
        return '<div class="col-span-1 md:col-span-2 text-center text-gray-500 py-8">표시할 시나리오가 없습니다.</div>'

    import time
    current_time = time.time()
    NEW_THRESHOLD = 30 * 60  # 30분 이내면 NEW 배지

    html = ""
    for info in file_infos:
        fid = info['filename']
        title = info['title']
        desc = info['desc']
        author = info['author']
        is_owner = info['is_owner']
        is_public = info['is_public']
        created_time = info.get('created_time', 0)

        # 생성 시간 포맷
        from datetime import datetime
        if created_time:
            created_dt = datetime.fromtimestamp(created_time)
            time_str = created_dt.strftime('%Y-%m-%d %H:%M')
        else:
            time_str = "알 수 없음"

        # NEW 배지 (30분 이내 생성)
        is_new = (current_time - created_time) < NEW_THRESHOLD if created_time else False
        new_badge = '<span class="ml-2 text-[10px] bg-red-500 text-white px-1.5 py-0.5 rounded-full font-bold animate-pulse">NEW</span>' if is_new else ''

        status_badge = '<span class="ml-2 text-[10px] bg-green-900 text-green-300 px-1 rounded">PUBLIC</span>' if is_public else '<span class="ml-2 text-[10px] bg-gray-700 text-gray-300 px-1 rounded">PRIVATE</span>'

        action_buttons = ""
        if is_owner:
            action_buttons += f"""
            <button onclick="deleteScenario('{fid}', this)" class="text-gray-500 hover:text-red-400 p-1"><i data-lucide="trash-2" class="w-4 h-4"></i></button>
            <button onclick="publishScenario('{fid}', this)" class="text-gray-500 hover:text-green-400 p-1 ml-1"><i data-lucide="share-2" class="w-4 h-4"></i></button>
            """

        html += f"""
        <div class="bg-dark-800 p-5 rounded-lg border border-white/5 hover:border-brand-purple/50 transition-colors flex flex-col justify-between h-full group relative">
            <div>
                <div class="flex justify-between items-start mb-2">
                    <h4 class="font-bold text-white text-lg flex items-center">{title} {new_badge} {status_badge if is_owner else ''}</h4>
                    <div class="opacity-0 group-hover:opacity-100 transition-opacity flex">{action_buttons}</div>
                </div>
                <div class="flex justify-between items-center text-xs text-gray-500 mb-1">
                    <span>{author}</span>
                    <span class="flex items-center gap-1"><i data-lucide="clock" class="w-3 h-3"></i>{time_str}</span>
                </div>
                <p class="text-sm text-gray-400 mb-4 line-clamp-2">{desc}</p>
            </div>
            <button onclick="playScenario('{fid}', this)"
                    class="w-full bg-brand-purple/10 hover:bg-brand-purple/20 text-brand-light py-2 rounded text-sm font-bold flex justify-center gap-2 border border-brand-purple/30 transition-all">
                <i data-lucide="play" class="w-4 h-4"></i> 플레이
            </button>
        </div>
        """
    html += '<script>lucide.createIcons();</script>'
    return html


@api_bp.route('/load_scenario', methods=['POST'])
def load_scenario():
    fid = request.form.get('filename')
    user_id = current_user.id if current_user.is_authenticated else None

    result, error = ScenarioService.load_scenario(fid, user_id)
    if error: return jsonify({"error": error}), 400

    scenario = result['scenario']
    start_id = pick_start_scene_id(scenario)

    game_state.config['title'] = scenario.get('title', 'Loaded')
    game_state.state = {
        "scenario": scenario,
        "current_scene_id": "prologue",
        "start_scene_id": start_id,
        "player_vars": result['player_vars'],
        "history": [], "last_user_choice_idx": -1, "system_message": "Loaded", "npc_output": "", "narrator_output": ""
    }
    game_state.game_graph = create_game_graph()

    return jsonify({"success": True})


@api_bp.route('/publish_scenario', methods=['POST'])
@login_required
def publish_scenario():
    data = request.get_json(force=True)
    fid = data.get('filename')
    success, msg = ScenarioService.publish_scenario(fid, current_user.id)
    return jsonify({"success": success, "message": msg, "error": msg})


@api_bp.route('/delete_scenario', methods=['POST'])
@login_required
def delete_scenario():
    data = request.get_json(force=True)
    fid = data.get('filename')
    success, msg = ScenarioService.delete_scenario(fid, current_user.id)
    return jsonify({"success": success, "message": msg, "error": msg})


@api_bp.route('/init_game', methods=['POST'])
def init_game():
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key: return jsonify({"error": "API Key 없음"}), 400

    react_flow_data = parse_request_data(request)
    selected_model = react_flow_data.get('model', 'openai/tngtech/deepseek-r1t2-chimera:free')

    update_build_progress(status="building", step="0/5", detail="준비 중...", progress=0)

    try:
        set_progress_callback(update_build_progress)

        scenario_json = generate_scenario_from_graph(api_key, react_flow_data, model_name=selected_model)

        user_id = current_user.id if current_user.is_authenticated else None
        fid, error = ScenarioService.save_scenario(scenario_json, user_id=user_id)

        if error:
            update_build_progress(status="error", detail=f"저장 오류: {error}")
            return jsonify({"error": error}), 500

        # 즉시 로드
        game_state.config['title'] = scenario_json.get('title')
        game_state.state = {
            "scenario": scenario_json,
            "current_scene_id": pick_start_scene_id(scenario_json),
            "player_vars": {}, "history": [], "last_user_choice_idx": -1, "system_message": "Init", "npc_output": "",
            "narrator_output": ""
        }
        game_state.game_graph = create_game_graph()

        update_build_progress(status="completed", step="완료", detail="생성 완료!", progress=100)
        return jsonify({"status": "success", "filename": fid})

    except Exception as e:
        logger.error(f"Init Error: {e}")
        update_build_progress(status="error", detail=str(e))
        return jsonify({"error": str(e)}), 500


# --- [빌더 도구 API] (신규 추가) ---
@api_bp.route('/builder/generate-npc', methods=['POST'])
def generate_npc_api():
    """NPC 생성 팝업에서 호출"""
    data = request.json
    scenario_title = data.get('scenario_title', '')
    scenario_summary = data.get('scenario_summary', '')
    user_request = data.get('user_request', '')

    if not scenario_title:
        return jsonify({"error": "시나리오 정보가 필요합니다."}), 400

    try:
        npc_data = generate_single_npc(scenario_title, scenario_summary, user_request)
        return jsonify(npc_data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- [프리셋 관리 (DB 기반)] ---

@api_bp.route('/presets', methods=['GET'])
def list_presets():
    """프리셋 목록 조회 API (JSON 반환)"""
    sort_order = request.args.get('sort', 'newest')
    limit = request.args.get('limit', type=int)

    user_id = current_user.id if current_user.is_authenticated else None

    file_infos = PresetService.list_presets(sort_order, user_id, limit)

    # JSON으로 반환 (React에서 사용)
    return jsonify(file_infos)


@api_bp.route('/presets/save', methods=['POST'])
@login_required
def save_preset():
    data = request.get_json(force=True, silent=True) or {}
    user_id = current_user.id if current_user.is_authenticated else None
    fid, error = PresetService.save_preset(data, user_id=user_id)
    if error: return jsonify({"success": False, "error": error}), 400
    return jsonify({"success": True, "filename": fid, "message": "프리셋이 저장되었습니다."})


@api_bp.route('/presets/load', methods=['POST'])
def load_preset_api():
    data = request.get_json(force=True, silent=True) or {}
    filename = data.get('filename')
    user_id = current_user.id if current_user.is_authenticated else None
    result, error = PresetService.load_preset(filename, user_id)
    if error: return jsonify({"success": False, "error": error}), 400
    preset = result['preset']
    return jsonify({
        "success": True,
        "data": preset,
        "message": f"'{preset.get('name')}' 프리셋을 불러왔습니다."
    })


@api_bp.route('/presets/delete', methods=['POST'])
@login_required
def delete_preset():
    data = request.get_json(force=True, silent=True) or {}
    fid = data.get('filename')
    success, msg = PresetService.delete_preset(fid, current_user.id)
    if success: return jsonify({"success": True, "message": "삭제 완료"})
    return jsonify({"success": False, "error": msg}), 400


@api_bp.route('/load_preset', methods=['POST'])
def load_preset_old():
    fid = request.form.get('filename')
    user_id = current_user.id if current_user.is_authenticated else None

    result, error = PresetService.load_preset(fid, user_id)
    if error: return f'<div class="bg-red-500/10 text-red-400 p-4 rounded-lg border border-red-500/20 shadow-lg fixed bottom-4 right-4 z-50">로드 실패: {error}</div>'

    preset = result['preset']

    game_state.config['title'] = preset.get('title', 'Loaded Preset')
    game_state.state = {
        "scenario": preset.get('scenario'),
        "current_scene_id": "prologue",
        "start_scene_id": "prologue",
        "player_vars": preset.get('player_vars', {}),
        "history": [], "last_user_choice_idx": -1, "system_message": "Loaded Preset", "npc_output": "",
        "narrator_output": ""
    }
    game_state.game_graph = create_game_graph()

    return f'''
    <div class="fixed bottom-4 right-4 z-50 flex flex-col gap-2 animate-bounce-in">
        <div class="bg-green-900/90 border border-green-500/30 text-green-100 px-6 py-4 rounded-xl shadow-2xl backdrop-blur-md flex items-center gap-4">
            <div class="bg-green-500/20 p-2 rounded-full">
                <i data-lucide="check" class="w-6 h-6 text-green-400"></i>
            </div>
            <div>
                <div class="font-bold text-lg">프리셋 로드 완료!</div>
                <div class="text-sm text-green-300/80">"{preset.get('title')}"</div>
            </div>
        </div>
        <a href="/views/player" 
           class="bg-brand-purple hover:bg-brand-light text-white py-4 px-6 rounded-xl font-bold text-lg shadow-xl shadow-brand-purple/20 transition-all flex items-center justify-center gap-3 transform hover:scale-[1.02]">
            <i data-lucide="play-circle" class="w-6 h-6"></i>
            게임 시작하기
        </a>
    </div>
    <script>lucide.createIcons();</script>
    '''