"""
API 라우트 (시나리오, 프리셋 관리)
"""
import os
import json
import logging
import time
from flask import Blueprint, request, jsonify

from config import DB_FOLDER
from core.state import game_state
from core.utils import parse_request_data, pick_start_scene_id
from services.scenario_service import ScenarioService
from services.preset_service import PresetService
from game_engine import create_game_graph

# builder_agent는 필요할 때만 import (순환 참조 방지)

logger = logging.getLogger(__name__)

api_bp = Blueprint('api', __name__, url_prefix='/api')


# --- 상태 관리 ---

@api_bp.route('/clear_state', methods=['POST'])
def clear_state():
    """새로고침 시 state 초기화"""
    game_state.clear()
    return jsonify({"status": "cleared"})


# --- 시나리오 관리 ---

@api_bp.route('/scenarios')
def list_scenarios():
    """시나리오 목록 조회 (HTML 반환)"""
    sort_order = request.args.get('sort', 'newest')
    file_infos = ScenarioService.list_scenarios(sort_order)

    if not file_infos:
        return '<div class="col-span-1 md:col-span-2 text-center text-gray-500 py-8">저장된 시나리오가 없습니다.</div>'

    html = ""
    for info in file_infos:
        f = info['filename']
        title = info['title']
        desc = info['desc']
        time_str = ScenarioService.format_time(info['created_time'])
        is_new = ScenarioService.is_recently_created(info['created_time'])

        new_badge = ""
        if is_new:
            new_badge = '<span class="ml-2 px-1.5 py-0.5 text-[10px] font-bold bg-yellow-500 text-black rounded">NEW</span>'

        html += f"""
        <div class="bg-gray-800 p-5 rounded-lg border border-gray-700 hover:border-indigo-500 transition-colors flex flex-col justify-between h-full group">
            <div>
                <div class="flex justify-between items-start mb-2">
                    <h4 class="font-bold text-white text-lg flex items-center">{title}{new_badge}</h4>
                    <button onclick="deleteScenario('{f}', this)" 
                            class="opacity-0 group-hover:opacity-100 text-gray-500 hover:text-red-400 p-1 rounded hover:bg-red-900/30 transition-all"
                            title="삭제">
                        <i data-lucide="trash-2" class="w-4 h-4"></i>
                    </button>
                </div>
                <div class="text-xs text-gray-500 mb-1">{f}</div>
                <div class="text-[10px] text-gray-600 mb-2">{time_str}</div>
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


@api_bp.route('/load_scenario', methods=['POST'])
def load_scenario():
    """시나리오 로드"""
    filename = request.form.get('filename')

    result, error = ScenarioService.load_scenario(filename)
    if error:
        return f'<div class="text-red-500">로드 실패: {error}</div>'

    scenario = result['scenario']
    initial_vars = result['player_vars']
    start_id = pick_start_scene_id(scenario)

    game_state.config['title'] = scenario.get('title', 'Loaded')
    game_state.state = {
        "scenario": scenario,
        "current_scene_id": "prologue",  # 프롤로그를 초기 위치로 설정
        "start_scene_id": start_id,  # 실제 시작 씬 ID 저장
        "player_vars": initial_vars,
        "history": [],
        "last_user_choice_idx": -1,
        "system_message": "Loaded",
        "npc_output": "",
        "narrator_output": ""
    }
    game_state.game_graph = create_game_graph()

    title = game_state.config['title']
    return f'''
    <div class="bg-green-900/30 border border-green-800 text-green-400 p-4 rounded-lg flex items-center gap-3 fade-in mt-4">
        <i data-lucide="check-circle" class="w-6 h-6"></i>
        <div>
            <div class="font-bold">"{title}" 로드 완료!</div>
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


@api_bp.route('/delete_scenario', methods=['POST'])
def delete_scenario():
    """시나리오 삭제"""
    data = request.get_json(force=True, silent=True) or {}
    filename = data.get('filename')

    success, error = ScenarioService.delete_scenario(filename)
    if not success:
        return jsonify({"success": False, "error": error}), 400 if error == "파일명이 없습니다." else 404

    return jsonify({"success": True, "message": f"'{filename}' 삭제 완료"})


@api_bp.route('/init_game', methods=['POST'])
def init_game():
    """빌더에서 시나리오 생성"""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return jsonify({"error": "API Key 없음"}), 400

    react_flow_data = parse_request_data(request)
    if not react_flow_data:
        return jsonify({"error": "유효하지 않은 데이터 형식"}), 400

    selected_model = react_flow_data.get('model', 'openai/tngtech/deepseek-r1t2-chimera:free')

    try:
        # 지연 import로 순환 참조 방지
        from builder_agent import generate_scenario_from_graph

        logging.info(f"Generating scenario from Graph... (Model: {selected_model})")
        scenario_json = generate_scenario_from_graph(api_key, react_flow_data, model_name=selected_model)

        # 문자열 응답 처리
        if isinstance(scenario_json, str):
            logger.warning(f"scenario_json is string. Parsing...")
            try:
                scenario_json = json.loads(scenario_json)
            except Exception as parse_error:
                logger.error(f"Failed to parse scenario_json: {parse_error}")
                return jsonify({"error": "생성된 데이터 형식이 잘못되었습니다."}), 500

        if not isinstance(scenario_json, dict):
            return jsonify({"error": "생성된 데이터가 딕셔너리가 아닙니다."}), 500

        # 저장
        filename, error = ScenarioService.save_scenario(scenario_json)
        if error:
            return jsonify({"error": f"저장 오류: {error}"}), 500

        title = scenario_json.get('title', 'Untitled_Scenario')
        start_id = pick_start_scene_id(scenario_json)

        # 플레이어 변수 초기화
        initial_vars = {}
        variables = scenario_json.get('variables', [])
        if isinstance(variables, list):
            for v in variables:
                if isinstance(v, dict):
                    initial_vars[v.get('name', 'unknown')] = v.get('initial_value', 0)

        if 'hp' not in initial_vars:
            initial_vars['hp'] = 100
        if 'inventory' not in initial_vars:
            initial_vars['inventory'] = []

        game_state.config['title'] = title
        game_state.state = {
            "scenario": scenario_json,
            "current_scene_id": start_id,
            "player_vars": initial_vars,
            "history": [],
            "last_user_choice_idx": -1,
            "system_message": "Init",
            "npc_output": "",
            "narrator_output": ""
        }
        game_state.game_graph = create_game_graph()

        return jsonify({
            "status": "success",
            "message": f"'{title}' 생성 완료! 플레이 탭으로 이동하세요.",
            "filename": filename
        })

    except Exception as e:
        logger.error(f"Error in init_game: {e}", exc_info=True)
        return jsonify({"error": f"생성 오류: {str(e)}"}), 500


# --- 프리셋 관리 ---

@api_bp.route('/presets')
def list_presets():
    """프리셋 목록 조회"""
    presets = PresetService.list_presets()
    return jsonify(presets)


@api_bp.route('/presets/save', methods=['POST'])
def save_preset():
    """프리셋 저장"""
    data = parse_request_data(request)
    if not data:
        return jsonify({"success": False, "error": "유효하지 않은 데이터"}), 400

    filename, error = PresetService.save_preset(data)
    if error:
        return jsonify({"success": False, "error": error}), 400

    return jsonify({
        "success": True,
        "message": f"프리셋 '{data.get('name')}' 저장 완료",
        "filename": filename
    })


@api_bp.route('/presets/load', methods=['POST'])
def load_preset():
    """프리셋 로드"""
    data = parse_request_data(request)
    filename = data.get('filename')

    preset_data, error = PresetService.load_preset(filename)
    if error:
        return jsonify({"success": False, "error": error}), 400 if "없습니다" in error else 404

    return jsonify({"success": True, "data": preset_data})


@api_bp.route('/presets/delete', methods=['POST'])
def delete_preset():
    """프리셋 삭제"""
    data = parse_request_data(request)
    filename = data.get('filename')

    success, error = PresetService.delete_preset(filename)
    if not success:
        return jsonify({"success": False, "error": error}), 400 if "없습니다" in error else 404

    return jsonify({"success": True, "message": "프리셋 삭제 완료"})
