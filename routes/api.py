import os
import json
import logging
import time
import threading
from flask import Blueprint, request, jsonify, Response, stream_with_context
from flask_login import login_user, logout_user, login_required, current_user

from core.state import game_state
from core.utils import parse_request_data, pick_start_scene_id
from services.scenario_service import ScenarioService
from services.preset_service import PresetService
from services.user_service import UserService
from game_engine import create_game_graph

logger = logging.getLogger(__name__)

api_bp = Blueprint('api', __name__, url_prefix='/api')


# --- [인증 API] ---

@api_bp.route('/auth/register', methods=['POST'])
def register():
    data = parse_request_data(request)
    username = data.get('username')
    password = data.get('password')

    if not username or not password:
        return jsonify({"success": False, "error": "아이디와 비밀번호를 입력해주세요."}), 400

    if UserService.create_user(username, password):
        return jsonify({"success": True, "message": "회원가입 성공! 로그인해주세요."})
    else:
        return jsonify({"success": False, "error": "이미 존재하는 아이디입니다."}), 400


@api_bp.route('/auth/login', methods=['POST'])
def login():
    data = parse_request_data(request)
    username = data.get('username')
    password = data.get('password')

    user = UserService.verify_user(username, password)
    if user:
        login_user(user)
        return jsonify({"success": True, "message": f"환영합니다, {username}님!"})
    else:
        return jsonify({"success": False, "error": "아이디 또는 비밀번호가 잘못되었습니다."}), 401


@api_bp.route('/auth/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return jsonify({"success": True, "message": "로그아웃 되었습니다."})


@api_bp.route('/auth/me', methods=['GET'])
def get_current_user():
    if current_user.is_authenticated:
        return jsonify({"is_logged_in": True, "username": current_user.id})
    else:
        return jsonify({"is_logged_in": False})


# --- [빌드 진행률] ---
build_progress = {
    "status": "idle", "step": "", "detail": "",
    "progress": 0, "total_scenes": 0, "completed_scenes": 0, "current_phase": ""
}
build_lock = threading.Lock()


def update_build_progress(status=None, step=None, detail=None, progress=None,
                          total_scenes=None, completed_scenes=None, current_phase=None):
    global build_progress
    with build_lock:
        if status is not None: build_progress["status"] = status
        if step is not None: build_progress["step"] = step
        if detail is not None: build_progress["detail"] = detail
        if progress is not None: build_progress["progress"] = progress
        if total_scenes is not None: build_progress["total_scenes"] = total_scenes
        if completed_scenes is not None: build_progress["completed_scenes"] = completed_scenes
        if current_phase is not None: build_progress["current_phase"] = current_phase


@api_bp.route('/build_progress')
def get_build_progress_sse():
    def generate():
        last_data = None
        while True:
            with build_lock:
                current_data = json.dumps(build_progress, ensure_ascii=False)
            if current_data != last_data:
                yield f"data: {current_data}\n\n"
                last_data = current_data
                if build_progress["status"] in ["completed", "error"]: break
            time.sleep(0.3)

    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# --- [시나리오 관리 (DB 기반)] ---

@api_bp.route('/scenarios')
def list_scenarios():
    sort_order = request.args.get('sort', 'newest')
    filter_mode = request.args.get('filter', 'public')  # public, my, all
    limit = request.args.get('limit', type=int)

    user_id = current_user.id if current_user.is_authenticated else None

    file_infos = ScenarioService.list_scenarios(sort_order, user_id, filter_mode, limit)

    if not file_infos:
        return '<div class="col-span-1 md:col-span-2 text-center text-gray-500 py-8">표시할 시나리오가 없습니다.</div>'

    html = ""
    for info in file_infos:
        fid = info['filename']
        title = info['title']
        desc = info['desc']
        author = info['author']
        is_owner = info['is_owner']
        is_public = info['is_public']

        status_badge = '<span class="ml-2 text-[10px] bg-green-900 text-green-300 px-1 rounded">PUBLIC</span>' if is_public else '<span class="ml-2 text-[10px] bg-gray-700 text-gray-300 px-1 rounded">PRIVATE</span>'

        action_buttons = ""
        if is_owner:
            action_buttons += f"""
            <button onclick="deleteScenario('{fid}', this)" 
                    class="text-gray-500 hover:text-red-400 p-1" title="삭제"><i data-lucide="trash-2" class="w-4 h-4"></i></button>
            <button onclick="publishScenario('{fid}', this)" 
                    class="text-gray-500 hover:text-green-400 p-1 ml-1" title="공개전환"><i data-lucide="share-2" class="w-4 h-4"></i></button>
            """

        html += f"""
        <div class="bg-dark-800 p-5 rounded-lg border border-white/5 hover:border-brand-purple/50 transition-colors flex flex-col justify-between h-full group relative">
            <div>
                <div class="flex justify-between items-start mb-2">
                    <h4 class="font-bold text-white text-lg flex items-center">{title} {status_badge if is_owner else ''}</h4>
                    <div class="opacity-0 group-hover:opacity-100 transition-opacity flex">{action_buttons}</div>
                </div>
                <div class="flex justify-between items-center text-xs text-gray-500 mb-1">
                    <span>{author}</span>
                </div>
                <p class="text-sm text-gray-400 mb-4 line-clamp-2">{desc}</p>
            </div>
            <button hx-post="/api/load_scenario" hx-vals='{{"filename": "{fid}"}}' hx-target="#init-result"
                    class="w-full bg-brand-purple/10 hover:bg-brand-purple/20 text-brand-light py-2 rounded text-sm font-bold flex justify-center gap-2 border border-brand-purple/30 transition-all">
                <i data-lucide="play" class="w-4 h-4"></i> 플레이
            </button>
        </div>
        """
    html += '<script>lucide.createIcons();</script>'
    return html


@api_bp.route('/load_scenario', methods=['POST'])
def load_scenario():
    fid = request.form.get('filename')  # DB ID
    user_id = current_user.id if current_user.is_authenticated else None

    result, error = ScenarioService.load_scenario(fid, user_id)
    if error: return f'<div class="bg-red-500/10 text-red-400 p-4 rounded-lg border border-red-500/20 shadow-lg fixed bottom-4 right-4 z-50">로드 실패: {error}</div>'

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

    # [수정] 버튼 클릭 시 함수 실행이 아니라 플레이어 페이지로 이동하도록 변경
    return f'''
    <div class="fixed bottom-4 right-4 z-50 flex flex-col gap-2 animate-bounce-in">
        <div class="bg-green-900/90 border border-green-500/30 text-green-100 px-6 py-4 rounded-xl shadow-2xl backdrop-blur-md flex items-center gap-4">
            <div class="bg-green-500/20 p-2 rounded-full">
                <i data-lucide="check" class="w-6 h-6 text-green-400"></i>
            </div>
            <div>
                <div class="font-bold text-lg">로드 완료!</div>
                <div class="text-sm text-green-300/80">"{scenario.get('title')}"</div>
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


@api_bp.route('/publish_scenario', methods=['POST'])
@login_required
def publish_scenario():
    data = request.get_json(force=True, silent=True) or {}
    fid = data.get('filename')
    success, msg = ScenarioService.publish_scenario(fid, current_user.id)
    if success: return jsonify({"success": True, "message": msg})
    return jsonify({"success": False, "error": msg}), 400


@api_bp.route('/delete_scenario', methods=['POST'])
@login_required
def delete_scenario():
    data = request.get_json(force=True, silent=True) or {}
    fid = data.get('filename')
    success, msg = ScenarioService.delete_scenario(fid, current_user.id)
    if success: return jsonify({"success": True, "message": "삭제 완료"})
    return jsonify({"success": False, "error": msg}), 400


@api_bp.route('/init_game', methods=['POST'])
def init_game():
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key: return jsonify({"error": "API Key 없음"}), 400

    react_flow_data = parse_request_data(request)
    selected_model = react_flow_data.get('model', 'openai/tngtech/deepseek-r1t2-chimera:free')

    update_build_progress(status="building", step="0/5", detail="준비 중...", progress=0)

    try:
        from builder_agent import generate_scenario_from_graph, set_progress_callback
        set_progress_callback(update_build_progress)

        scenario_json = generate_scenario_from_graph(api_key, react_flow_data, model_name=selected_model)

        user_id = current_user.id if current_user.is_authenticated else None

        # DB 저장
        fid, error = ScenarioService.save_scenario(scenario_json, user_id=user_id)

        if error:
            update_build_progress(status="error", detail=f"저장 오류: {error}")
            return jsonify({"error": error}), 500

        # 즉시 로드
        game_state.config['title'] = scenario_json.get('title')
        game_state.state = {
            "scenario": scenario_json,
            "current_scene_id": pick_start_scene_id(scenario_json),
            "player_vars": {},  # 초기화 생략
            "history": [], "last_user_choice_idx": -1, "system_message": "Init", "npc_output": "", "narrator_output": ""
        }
        game_state.game_graph = create_game_graph()

        update_build_progress(status="completed", step="완료", detail="생성 완료!", progress=100)
        return jsonify({"status": "success", "filename": fid})

    except Exception as e:
        logger.error(f"Init Error: {e}")
        update_build_progress(status="error", detail=str(e))
        return jsonify({"error": str(e)}), 500


# --- [프리셋 관리 (DB 기반)] ---

@api_bp.route('/presets', methods=['GET'])
def list_presets():
    sort_order = request.args.get('sort', 'newest')
    limit = request.args.get('limit', type=int)

    user_id = current_user.id if current_user.is_authenticated else None

    file_infos = PresetService.list_presets(sort_order, user_id, limit)

    if not file_infos:
        return '<div class="col-span-1 md:col-span-2 text-center text-gray-500 py-8">표시할 프리셋이 없습니다.</div>'

    html = ""
    for info in file_infos:
        fid = info['filename']
        title = info['title']
        desc = info['desc']
        author = info['author']
        is_owner = info['is_owner']

        action_buttons = ""
        if is_owner:
            action_buttons += f"""
            <button onclick="deletePreset('{fid}', this)" 
                    class="text-gray-500 hover:text-red-400 p-1" title="삭제"><i data-lucide="trash-2" class="w-4 h-4"></i></button>
            """

        html += f"""
        <div class="bg-dark-800 p-5 rounded-lg border border-white/5 hover:border-brand-purple/50 transition-colors flex flex-col justify-between h-full group relative">
            <div>
                <div class="flex justify-between items-start mb-2">
                    <h4 class="font-bold text-white text-lg flex items-center">{title}</h4>
                    <div class="opacity-0 group-hover:opacity-100 transition-opacity flex">{action_buttons}</div>
                </div>
                <div class="flex justify-between items-center text-xs text-gray-500 mb-1">
                    <span>{author}</span>
                </div>
                <p class="text-sm text-gray-400 mb-4 line-clamp-2">{desc}</p>
            </div>
            <button hx-post="/api/load_preset" hx-vals='{{"filename": "{fid}"}}' hx-target="#init-result"
                    class="w-full bg-brand-purple/10 hover:bg-brand-purple/20 text-brand-light py-2 rounded text-sm font-bold flex justify-center gap-2 border border-brand-purple/30 transition-all">
                <i data-lucide="play" class="w-4 h-4"></i> 적용하기
            </button>
        </div>
        """
    html += '<script>lucide.createIcons();</script>'
    return html


@api_bp.route('/save_preset', methods=['POST'])
@login_required
def save_preset():
    """프리셋 저장 API"""
    data = request.get_json(force=True, silent=True) or {}

    user_id = current_user.id if current_user.is_authenticated else None

    fid, error = PresetService.save_preset(data, user_id=user_id)

    if error:
        return jsonify({"success": False, "error": error}), 400

    return jsonify({"success": True, "filename": fid, "message": "프리셋이 저장되었습니다."})


@api_bp.route('/load_preset', methods=['POST'])
def load_preset():
    fid = request.form.get('filename')  # DB ID
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
        "history": [], "last_user_choice_idx": -1, "system_message": "Loaded Preset", "npc_output": "", "narrator_output": ""
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


@api_bp.route('/delete_preset', methods=['POST'])
@login_required
def delete_preset():
    data = request.get_json(force=True, silent=True) or {}
    fid = data.get('filename')
    success, msg = PresetService.delete_preset(fid, current_user.id)
    if success: return jsonify({"success": True, "message": "삭제 완료"})
    return jsonify({"success": False, "error": msg}), 400
