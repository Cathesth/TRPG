import os
import json
import logging
import time
import threading
import glob
from typing import Optional, List, Dict, Any
from flask import Blueprint, request, jsonify, Response, stream_with_context
from flask_login import login_user, logout_user, login_required, current_user

# Core & Services
from core.state import game_state
from core.utils import parse_request_data, pick_start_scene_id
from services.scenario_service import ScenarioService
from services.preset_service import PresetService
from services.user_service import UserService
from game_engine import create_game_graph

logger = logging.getLogger(__name__)

api_bp = Blueprint('api', __name__, url_prefix='/api')


# ==========================================
# [인증 API]
# ==========================================
@api_bp.route('/auth/register', methods=['POST'])
def register():
    data = parse_request_data(request)
    username = data.get('username')
    password = data.get('password')
    if not username or not password: return jsonify({"success": False, "error": "입력값 부족"}), 400
    if UserService.create_user(username, password): return jsonify({"success": True})
    return jsonify({"success": False, "error": "이미 존재하는 아이디"}), 400


@api_bp.route('/auth/login', methods=['POST'])
def login():
    data = parse_request_data(request)
    user = UserService.verify_user(data.get('username'), data.get('password'))
    if user:
        login_user(user)
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "로그인 실패"}), 401


@api_bp.route('/auth/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return jsonify({"success": True})


@api_bp.route('/auth/me', methods=['GET'])
def get_current_user_info():
    return jsonify({
        "is_logged_in": current_user.is_authenticated,
        "username": current_user.id if current_user.is_authenticated else None
    })


# ==========================================
# [빌드 진행률 SSE]
# ==========================================
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


# ==========================================
# [시나리오 관리 - 핵심 수정]
# ==========================================
@api_bp.route('/scenarios')
def list_scenarios():
    """
    시나리오 목록 반환 (DB + JSON 파일 통합)
    """
    sort_order = request.args.get('sort', 'newest')
    filter_mode = request.args.get('filter', 'public')
    limit = request.args.get('limit', type=int)

    current_user_id = current_user.id if current_user.is_authenticated else None

    file_infos = []

    # 1. 파일 시스템에서 JSON 시나리오 읽기 (DB/scenarios 폴더)
    # (필터가 'my'가 아니거나, 전체 보기일 때 포함)
    if filter_mode in ['public', 'all']:
        try:
            base_path = os.path.join("DB", "scenarios")
            if not os.path.exists(base_path):
                os.makedirs(base_path, exist_ok=True)

            json_files = glob.glob(os.path.join(base_path, "*.json"))

            for file_path in json_files:
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)

                    # 파일명(ID 역할)
                    filename = os.path.basename(file_path).replace('.json', '')

                    info = {
                        'filename': filename,  # 로드할 때 이 값을 사용
                        'title': data.get('title', filename),
                        'desc': data.get('desc', '') or data.get('description', '설명 없음'),
                        'author': data.get('author', 'System'),
                        'created_time': os.path.getctime(file_path),
                        'image': data.get('image', ''),
                        'is_public': True,  # 파일로 있는건 기본 공개로 처리
                        'is_owner': False,
                        'views': data.get('views', 0),
                        'clicks': data.get('clicks', 0),
                        'plays': data.get('plays', 0)
                    }
                    file_infos.append(info)
                except Exception as e:
                    logger.error(f"Failed to load scenario file {file_path}: {e}")
        except Exception as e:
            logger.error(f"Error scanning scenario directory: {e}")

    # 2. DB에서 시나리오 읽기
    # Service가 내부적으로 필터링을 수행함
    db_infos = ScenarioService.list_scenarios(sort_order, current_user_id, filter_mode, None)
    if db_infos:
        file_infos.extend(db_infos)

    # 3. 데이터 정렬 (통합 후 정렬)
    if sort_order == 'popular':
        file_infos.sort(key=lambda x: x.get('views', 0) + x.get('clicks', 0), reverse=True)
    elif sort_order == 'steady':
        file_infos.sort(key=lambda x: x.get('plays', 0), reverse=True)
    elif sort_order == 'name_asc':
        file_infos.sort(key=lambda x: x.get('title', ''))
    else:  # newest
        file_infos.sort(key=lambda x: x.get('created_time', 0), reverse=True)

    # 4. 개수 제한
    if limit:
        file_infos = file_infos[:limit]

    if not file_infos:
        return '<div class="col-span-full text-center text-gray-500 py-12">표시할 시나리오가 없습니다.</div>'

    # 5. HTML 생성
    from datetime import datetime
    current_time = time.time()
    NEW_THRESHOLD = 30 * 60  # 30분

    html = ""
    for info in file_infos:
        fid = info['filename']
        title = info['title']
        desc = info['desc']
        author = info['author']
        is_owner = info['is_owner']
        is_public = info.get('is_public', False)
        created_time = info.get('created_time', 0)
        img_src = info.get('image') or "https://images.unsplash.com/photo-1519074069444-1ba4fff66d16?q=80&w=800"

        # 시간 포맷
        time_str = datetime.fromtimestamp(created_time).strftime('%Y-%m-%d') if created_time else "-"

        # 뱃지
        is_new = (current_time - created_time) < NEW_THRESHOLD
        new_badge = '<span class="ml-2 text-[10px] bg-red-500 text-white px-1.5 py-0.5 rounded-full font-bold animate-pulse">NEW</span>' if is_new else ''
        status_badge = f'<span class="ml-2 text-[10px] {"bg-green-900 text-green-300" if is_public else "bg-gray-700 text-gray-300"} px-1 rounded font-bold">{"PUBLIC" if is_public else "PRIVATE"}</span>' if is_owner else ''

        # 버튼
        admin_buttons = ""
        if is_owner:
            admin_buttons = f"""
            <div class="flex gap-2 mt-3 pt-3 border-t border-white/10">
                <button onclick="editScenario('{fid}')" class="flex-1 py-2 rounded-lg bg-rpg-900/50 border border-rpg-700 hover:border-rpg-accent text-gray-400 hover:text-white transition-colors flex items-center justify-center gap-1">
                    <i data-lucide="edit" class="w-3 h-3"></i> <span class="text-xs">EDIT</span>
                </button>
                <button onclick="deleteScenario('{fid}', this)" class="flex-1 py-2 rounded-lg bg-rpg-900/50 border border-rpg-700 hover:border-danger hover:text-danger text-gray-400 transition-colors flex items-center justify-center gap-1">
                    <i data-lucide="trash" class="w-3 h-3"></i> <span class="text-xs">DEL</span>
                </button>
            </div>
            """

        # index.html의 .scenario-card-base 클래스 사용 (다크모드 대응)
        html += f"""
        <div class="scenario-card-base group">
            <div class="card-image-wrapper">
                <img src="{img_src}" class="card-image" alt="Cover">
                <div class="absolute top-3 left-3 bg-black/70 backdrop-blur px-2 py-1 rounded text-[10px] font-bold text-rpg-accent border border-rpg-accent/30">
                    Fantasy
                </div>
            </div>

            <div class="card-content">
                <div>
                    <div class="flex justify-between items-start mb-1">
                        <h3 class="card-title text-white group-hover:text-rpg-accent transition-colors">{title} {new_badge}</h3>
                        {status_badge}
                    </div>
                    <div class="flex justify-between items-center text-xs text-gray-400 mb-2">
                        <span>{author}</span>
                        <span class="flex items-center gap-1"><i data-lucide="clock" class="w-3 h-3"></i>{time_str}</span>
                    </div>
                    <p class="card-desc text-gray-400">{desc}</p>
                </div>

                <button onclick="playScenario('{fid}', this)" class="w-full py-3 bg-rpg-accent/10 hover:bg-rpg-accent text-rpg-accent hover:text-black font-bold rounded-lg transition-all flex items-center justify-center gap-2 border border-rpg-accent/50 mt-auto shadow-[0_0_10px_rgba(56,189,248,0.1)] hover:shadow-[0_0_15px_rgba(56,189,248,0.4)]">
                    <i data-lucide="play" class="w-4 h-4 fill-current"></i> PLAY NOW
                </button>

                {admin_buttons}
            </div>
        </div>
        """

    html += '<script>lucide.createIcons();</script>'
    return html


@api_bp.route('/load_scenario', methods=['POST'])
def load_scenario():
    """
    시나리오 로드 (DB or File)
    """
    fid = request.form.get('filename')
    user_id = current_user.id if current_user.is_authenticated else None

    scenario = None
    player_vars = {}

    # 1. 파일에서 찾기 시도
    # (fid가 DB ID(숫자)가 아닌 경우 혹은 파일이 존재하는 경우)
    file_path = os.path.join("DB", "scenarios", f"{fid}.json")
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                scenario = data
                player_vars = data.get('player_vars', {})
        except Exception as e:
            logger.error(f"File load error: {e}")

    # 2. 파일에 없으면 DB에서 찾기
    if not scenario:
        result, error = ScenarioService.load_scenario(fid, user_id)
        if not error:
            scenario = result['scenario']
            player_vars = result['player_vars']
        else:
            # DB 로드 실패 시 에러 반환
            return jsonify({"error": f"시나리오를 찾을 수 없습니다. ({error})"}), 400

    if not scenario:
        return jsonify({"error": "Scenario data is empty"}), 400

    # 게임 상태 초기화
    start_id = pick_start_scene_id(scenario)
    game_state.config['title'] = scenario.get('title', 'Loaded')

    # WorldState 초기화 (싱글톤)
    from core.state import WorldState
    world_state_instance = WorldState()
    world_state_instance.reset()
    world_state_instance.initialize_from_scenario(scenario)

    game_state.state = {
        "scenario": scenario,  # 전체 데이터 포함 (Legacy 지원)
        "current_scene_id": "prologue",
        "start_scene_id": start_id,
        "player_vars": player_vars,
        "history": [],
        "last_user_choice_idx": -1,
        "system_message": "Loaded",
        "npc_output": "",
        "narrator_output": ""
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
        from builder_agent import generate_scenario_from_graph, set_progress_callback
        set_progress_callback(update_build_progress)

        scenario_json = generate_scenario_from_graph(api_key, react_flow_data, model_name=selected_model)

        user_id = current_user.id if current_user.is_authenticated else None
        fid, error = ScenarioService.save_scenario(scenario_json, user_id=user_id)

        if error:
            update_build_progress(status="error", detail=f"저장 오류: {error}")
            return jsonify({"error": error}), 500

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


# ... (프리셋 관련 API는 기존과 동일하게 유지하거나 필요 시 추가) ...
@api_bp.route('/presets', methods=['GET'])
def list_presets():
    sort_order = request.args.get('sort', 'newest')
    limit = request.args.get('limit', type=int)
    file_infos = PresetService.list_presets(sort_order, None, limit)
    return jsonify(file_infos)


@api_bp.route('/presets/save', methods=['POST'])
@login_required
def save_preset():
    data = request.get_json(force=True, silent=True) or {}
    fid, error = PresetService.save_preset(data, user_id=current_user.id)
    if error: return jsonify({"success": False, "error": error}), 400
    return jsonify({"success": True, "filename": fid})


@api_bp.route('/presets/load', methods=['POST'])
def load_preset():
    data = request.get_json(force=True, silent=True) or {}
    result, error = PresetService.load_preset(data.get('filename'), None)
    if error: return jsonify({"success": False, "error": error}), 400
    return jsonify({"success": True, "data": result['preset']})


@api_bp.route('/presets/delete', methods=['POST'])
@login_required
def delete_preset():
    data = request.get_json(force=True, silent=True) or {}
    success, msg = PresetService.delete_preset(data.get('filename'), current_user.id)
    if success: return jsonify({"success": True})
    return jsonify({"success": False, "error": msg}), 400