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
from core.utils import parse_request_data, pick_start_scene_id, validate_scenario_graph, can_publish_scenario
from services.scenario_service import ScenarioService
from services.user_service import UserService
from services.preset_service import PresetService
from services.draft_service import DraftService
# [추가] NPC 저장을 위한 서비스 임포트 (이전 답변의 npc_service.py 필요)
from services.npc_service import save_custom_npc

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
    email = data.get('email')
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

        # HTML 엔티티 이스케이프 (XSS 방지 및 JavaScript 문자열 안전성)
        title_escaped = title.replace("'", "\\'").replace('"', '&quot;')

        action_buttons = ""
        if is_owner:
            action_buttons += f"""
            <button onclick="editScenario('{fid}')" class="text-gray-500 hover:text-blue-400 p-1" title="수정"><i data-lucide="pencil" class="w-4 h-4"></i></button>
            <button onclick="publishScenario('{fid}', this)" class="text-gray-500 hover:text-green-400 p-1" title="공유"><i data-lucide="share-2" class="w-4 h-4"></i></button>
            <button onclick="deleteScenario('{fid}', '{title_escaped}', this)" class="text-gray-500 hover:text-red-400 p-1" title="삭제"><i data-lucide="trash-2" class="w-4 h-4"></i></button>
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


@api_bp.route('/scenario/<scenario_id>/edit', methods=['GET'])
@login_required
def get_scenario_for_edit(scenario_id):
    """편집용 시나리오 데이터 로드"""
    result, error = ScenarioService.get_scenario_for_edit(scenario_id, current_user.id)
    if error:
        return jsonify({"success": False, "error": error}), 403
    return jsonify({"success": True, "data": result})


@api_bp.route('/scenario/<scenario_id>/update', methods=['POST'])
@login_required
def update_scenario(scenario_id):
    """시나리오 업데이트 (편집 모드)"""
    data = request.get_json(force=True)
    success, error = ScenarioService.update_scenario(scenario_id, data, current_user.id)
    if not success:
        return jsonify({"success": False, "error": error}), 400
    return jsonify({"success": True, "message": "저장되었습니다."})


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
        return jsonify({"status": "success", "filename": fid, **scenario_json})  # [수정] 프론트엔드 모달 표시를 위해 전체 데이터 반환

    except Exception as e:
        logger.error(f"Init Error: {e}")
        update_build_progress(status="error", detail=str(e))
        return jsonify({"error": str(e)}), 500


# --- [NPC/Enemy 생성 및 저장 API] (수정됨) ---

@api_bp.route('/npc/generate', methods=['POST'])
def generate_npc_api():
    """
    [Agent 연결] NPC 생성 팝업에서 AI 생성을 요청할 때 호출
    npc_generator.html의 '/api/npc/generate' fetch 요청 대응
    """
    data = request.json
    scenario_title = data.get('scenario_title', 'Unknown Scenario')
    scenario_summary = data.get('scenario_summary', '')
    user_request = data.get('request', '')  # 프론트엔드에서 'request' 키로 전송됨
    model_name = data.get('model')  # 선택적 모델 지정

    try:
        # Builder Agent의 함수 호출
        npc_data = generate_single_npc(scenario_title, scenario_summary, user_request, model_name)
        return jsonify({"success": True, "data": npc_data})
    except Exception as e:
        logger.error(f"NPC Generation Error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@api_bp.route('/npc/save', methods=['POST'])
def save_npc():
    """
    [DB 연결] 사용자가 작성한 NPC/Enemy 데이터를 JSON DB에 저장
    npc_generator.html의 '/api/npc/save' fetch 요청 대응
    """
    try:
        data = request.json
        if not data:
            return jsonify({"success": False, "error": "No data provided"}), 400

        # 서비스 계층을 통해 JSON 파일에 저장
        saved_entity = save_custom_npc(data)

        return jsonify({
            "success": True,
            "message": "저장되었습니다.",
            "data": saved_entity
        })
    except Exception as e:
        logger.error(f"NPC Save Error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


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


# --- [Draft 시스템 API (비주얼 에디터용)] ---

from services.mermaid_service import MermaidService

def _generate_mermaid_for_response(scenario_data):
    """응답용 Mermaid 코드 생성"""
    try:
        chart_data = MermaidService.generate_chart(scenario_data, None)
        return chart_data.get('mermaid_code', '')
    except Exception as e:
        logger.error(f"Mermaid generation error: {e}")
        return ''

@api_bp.route('/draft/<int:scenario_id>', methods=['GET'])
@login_required
def get_draft(scenario_id):
    """Draft 로드 (없으면 원본 시나리오 데이터 반환)"""
    result, error = DraftService.get_draft(scenario_id, current_user.id)
    if error:
        return jsonify({"success": False, "error": error}), 403

    # Mermaid 코드 포함하여 응답
    mermaid_code = _generate_mermaid_for_response(result['scenario'])
    return jsonify({"success": True, "mermaid_code": mermaid_code, **result})


@api_bp.route('/draft/<int:scenario_id>/save', methods=['POST'])
@login_required
def save_draft(scenario_id):
    """Draft 저장 (자동 저장용)"""
    data = request.get_json(force=True)
    success, error = DraftService.save_draft(scenario_id, current_user.id, data)
    if not success:
        return jsonify({"success": False, "error": error}), 400
    return jsonify({"success": True, "message": "Draft가 저장되었습니다."})


@api_bp.route('/draft/<int:scenario_id>/publish', methods=['POST'])
@login_required
def publish_draft(scenario_id):
    """Draft를 실제 시나리오로 최종 반영 (유효성 검사 통과 필수)"""
    data = request.get_json(force=True, silent=True) or {}
    force = data.get('force', False)  # 강제 반영 옵션

    success, error, validation_result = DraftService.publish_draft(scenario_id, current_user.id, force=force)

    if not success:
        return jsonify({
            "success": False,
            "error": error,
            "validation": validation_result
        }), 400

    return jsonify({
        "success": True,
        "message": "시나리오에 최종 반영되었습니다.",
        "validation": validation_result
    })


@api_bp.route('/draft/<int:scenario_id>/validate', methods=['GET', 'POST'])
@login_required
def validate_draft(scenario_id):
    """
    시나리오 그래프 유효성 검사 API

    검사 항목:
    - 고립 노드: 어떤 경로로도 도달할 수 없는 씬 적발
    - 참조 무결성: 존재하지 않는 ID를 가리키는 target_scene_id 적발
    - 도달 가능성: 시작 씬에서 하나 이상의 엔딩까지 도달 가능한 경로 존재 여부
    """
    result, error = DraftService.get_draft(scenario_id, current_user.id)
    if error:
        return jsonify({"success": False, "error": error}), 403

    scenario_data = result['scenario']

    # 유효성 검사 수행
    validation_result = validate_scenario_graph(scenario_data)

    return jsonify({
        "success": True,
        "can_publish": validation_result.is_valid,
        "validation": validation_result.to_dict()
    })


@api_bp.route('/draft/<int:scenario_id>/discard', methods=['POST'])
@login_required
def discard_draft(scenario_id):
    """Draft 폐기 (변경사항 취소)"""
    success, error = DraftService.discard_draft(scenario_id, current_user.id)
    if not success:
        return jsonify({"success": False, "error": error}), 400
    return jsonify({"success": True, "message": "변경사항이 취소되었습니다."})


@api_bp.route('/draft/<int:scenario_id>/reorder', methods=['POST'])
@login_required
def reorder_scene_ids(scenario_id):
    """씬 ID 순차 재정렬"""
    result, error = DraftService.get_draft(scenario_id, current_user.id)
    if error:
        return jsonify({"success": False, "error": error}), 403

    scenario_data = result['scenario']
    reordered_data, id_mapping = DraftService.reorder_scene_ids(scenario_data)

    if not id_mapping:
        return jsonify({"success": True, "message": "재정렬할 필요가 없습니다.", "changes": 0})

    # Draft에 저장
    success, save_error = DraftService.save_draft(scenario_id, current_user.id, reordered_data)
    if not success:
        return jsonify({"success": False, "error": save_error}), 400

    return jsonify({
        "success": True,
        "message": f"{len(id_mapping)}개의 씬 ID가 재정렬되었습니다.",
        "id_mapping": id_mapping,
        "scenario": reordered_data
    })


@api_bp.route('/draft/<int:scenario_id>/check-references', methods=['POST'])
@login_required
def check_scene_references(scenario_id):
    """씬 삭제 전 참조 확인"""
    data = request.get_json(force=True)
    scene_id = data.get('scene_id')

    if not scene_id:
        return jsonify({"success": False, "error": "scene_id가 필요합니다."}), 400

    result, error = DraftService.get_draft(scenario_id, current_user.id)
    if error:
        return jsonify({"success": False, "error": error}), 403

    references = DraftService.check_scene_references(result['scenario'], scene_id)

    return jsonify({
        "success": True,
        "scene_id": scene_id,
        "references": references,
        "has_references": len(references) > 0
    })


@api_bp.route('/draft/<int:scenario_id>/delete-scene', methods=['POST'])
@login_required
def delete_scene(scenario_id):
    """씬 삭제"""
    data = request.get_json(force=True)
    scene_id = data.get('scene_id')
    handle_mode = data.get('handle_mode', 'remove_transitions')

    if not scene_id:
        return jsonify({"success": False, "error": "scene_id가 필요합니다."}), 400

    result, error = DraftService.get_draft(scenario_id, current_user.id)
    if error:
        return jsonify({"success": False, "error": error}), 403

    updated_scenario, warnings = DraftService.delete_scene(result['scenario'], scene_id, handle_mode)

    # Draft에 저장
    success, save_error = DraftService.save_draft(scenario_id, current_user.id, updated_scenario)
    if not success:
        return jsonify({"success": False, "error": save_error}), 400

    return jsonify({
        "success": True,
        "message": f"씬 '{scene_id}'이(가) 삭제되었습니다.",
        "warnings": warnings,
        "scenario": updated_scenario
    })


@api_bp.route('/draft/<int:scenario_id>/add-scene', methods=['POST'])
@login_required
def add_scene(scenario_id):
    """새 씬 추가"""
    data = request.get_json(force=True)
    new_scene = data.get('scene', {})
    after_scene_id = data.get('after_scene_id')

    result, error = DraftService.get_draft(scenario_id, current_user.id)
    if error:
        return jsonify({"success": False, "error": error}), 403

    updated_scenario = DraftService.add_scene(result['scenario'], new_scene, after_scene_id)

    # Draft에 저장
    success, save_error = DraftService.save_draft(scenario_id, current_user.id, updated_scenario)
    if not success:
        return jsonify({"success": False, "error": save_error}), 400

    # 생성된 씬 ID 반환
    added_scene = updated_scenario['scenes'][-1] if not after_scene_id else None
    if after_scene_id:
        for i, s in enumerate(updated_scenario['scenes']):
            if s.get('scene_id') == after_scene_id and i + 1 < len(updated_scenario['scenes']):
                added_scene = updated_scenario['scenes'][i + 1]
                break

    return jsonify({
        "success": True,
        "message": "새 씬이 추가되었습니다.",
        "scene": added_scene,
        "scenario": updated_scenario
    })


@api_bp.route('/draft/<int:scenario_id>/add-ending', methods=['POST'])
@login_required
def add_ending(scenario_id):
    """새 엔딩 추가"""
    data = request.get_json(force=True)
    new_ending = data.get('ending', {})

    result, error = DraftService.get_draft(scenario_id, current_user.id)
    if error:
        return jsonify({"success": False, "error": error}), 403

    updated_scenario = DraftService.add_ending(result['scenario'], new_ending)

    # Draft에 저장
    success, save_error = DraftService.save_draft(scenario_id, current_user.id, updated_scenario)
    if not success:
        return jsonify({"success": False, "error": save_error}), 400

    added_ending = updated_scenario['endings'][-1]

    return jsonify({
        "success": True,
        "message": "새 엔딩이 추가되었습니다.",
        "ending": added_ending,
        "scenario": updated_scenario
    })


@api_bp.route('/draft/<int:scenario_id>/delete-ending', methods=['POST'])
@login_required
def delete_ending(scenario_id):
    """엔딩 삭제"""
    data = request.get_json(force=True)
    ending_id = data.get('ending_id')

    if not ending_id:
        return jsonify({"success": False, "error": "ending_id가 필요합니다."}), 400

    result, error = DraftService.get_draft(scenario_id, current_user.id)
    if error:
        return jsonify({"success": False, "error": error}), 403

    updated_scenario, warnings = DraftService.delete_ending(result['scenario'], ending_id)

    # Draft에 저장
    success, save_error = DraftService.save_draft(scenario_id, current_user.id, updated_scenario)
    if not success:
        return jsonify({"success": False, "error": save_error}), 400

    return jsonify({
        "success": True,
        "message": f"엔딩 '{ending_id}'이(가) 삭제되었습니다.",
        "warnings": warnings,
        "scenario": updated_scenario
    })
