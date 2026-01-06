from flask import Blueprint, render_template
from flask_login import login_required, current_user

from core.state import game_state
from services.mermaid_service import MermaidService
from config import get_full_version

views_bp = Blueprint('views', __name__)


@views_bp.route('/')
def index():
    """메인 페이지"""
    return render_template('index.html', version=get_full_version(), user=current_user)


@views_bp.route('/views/builder')
@login_required
def view_builder():
    """빌더 뷰"""
    return render_template('builder_view.html', version=get_full_version(), user=current_user)


@views_bp.route('/views/player')
def view_player():
    """플레이어 뷰"""
    p_vars = {}
    if game_state.state:
        p_vars = game_state.state.get('player_vars', {})
    return render_template('player_view.html', vars=p_vars, version=get_full_version(), user=current_user)


@views_bp.route('/views/scenes')
def view_scenes():
    """씬 맵 뷰"""
    if not game_state.state:
        return render_template('scenes_view.html',
                               title="시나리오 없음",
                               scenario={"endings": [], "prologue_text": ""},
                               scenes=[],
                               current_scene_id=None,
                               mermaid_code="graph TD\n    A[시나리오를 먼저 로드하세요]",
                               scene_display_ids={},
                               ending_display_ids={},
                               version=get_full_version(),
                               user=current_user)

    scenario = game_state.state['scenario']
    title = scenario.get('title', 'Untitled')

    # 현재 씬 ID 가져오기
    current_scene_id = game_state.state.get('current_scene_id', None)

    # Mermaid 서비스로 차트 생성 (현재 씬 ID 전달)
    chart_data = MermaidService.generate_chart(scenario, current_scene_id)

    return render_template('scenes_view.html',
                           title=title,
                           scenario=scenario,
                           scenes=chart_data['filtered_scenes'],
                           incoming_conditions=chart_data['incoming_conditions'],
                           ending_incoming_conditions=chart_data['ending_incoming_conditions'],
                           ending_names=chart_data['ending_names'],
                           scene_names=chart_data['scene_names'],
                           scene_display_ids=chart_data['scene_display_ids'],
                           ending_display_ids=chart_data['ending_display_ids'],
                           current_scene_id=current_scene_id,
                           mermaid_code=chart_data['mermaid_code'],
                           version=get_full_version(),
                           user=current_user)


# [유지] NPC 생성 팝업 라우트 (builder_view.html의 JS에서 호출함)
@views_bp.route('/builder/npc-generator')
def npc_generator():
    """NPC 생성 팝업창"""
    return render_template('npc_generator.html')