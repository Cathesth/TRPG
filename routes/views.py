"""
뷰 렌더링 라우트 (Mermaid + Login 통합 버전)
"""
from flask import Blueprint, render_template
from flask_login import login_required, current_user  # [추가] 로그인 관련 모듈

from core.state import game_state
from services.mermaid_service import MermaidService
from config import get_full_version

views_bp = Blueprint('views', __name__)


@views_bp.route('/')
def index():
    """메인 페이지"""
    # [수정] 템플릿에 user 정보 전달 (로그인/로그아웃 버튼 표시용)
    return render_template('index.html', version=get_full_version(), user=current_user)


@views_bp.route('/views/builder')
@login_required  # [추가] 빌더는 로그인한 유저만 접근 가능
def view_builder():
    """빌더 뷰"""
    return render_template('builder_view.html', user=current_user)


@views_bp.route('/views/player')
def view_player():
    """플레이어 뷰"""
    p_vars = {}
    if game_state.state:
        p_vars = game_state.state.get('player_vars', {})
    # [수정] user 정보 전달
    return render_template('player_view.html', vars=p_vars, user=current_user)


@views_bp.route('/views/scenes')
def view_scenes():
    """씬 맵 뷰"""
    # [수정] user 정보 공통 전달
    if not game_state.state:
        return render_template('scenes_view.html',
                               title="시나리오 없음",
                               scenario={"endings": [], "prologue_text": ""},
                               scenes=[],
                               current_scene_id=None,
                               mermaid_code="graph TD\n    A[시나리오를 먼저 로드하세요]",
                               user=current_user)

    scenario = game_state.state['scenario']
    title = scenario.get('title', 'Untitled')

    # 현재 씬 ID 가져오기
    current_scene_id = game_state.state.get('current_scene_id', None)

    # Mermaid 서비스로 차트 생성
    chart_data = MermaidService.generate_chart(scenario)

    return render_template('scenes_view.html',
                           title=title,
                           scenario=scenario,
                           scenes=chart_data['filtered_scenes'],
                           incoming_conditions=chart_data['incoming_conditions'],
                           ending_incoming_conditions=chart_data['ending_incoming_conditions'],
                           ending_names=chart_data['ending_names'],
                           scene_names=chart_data['scene_names'],
                           current_scene_id=current_scene_id,
                           mermaid_code=chart_data['mermaid_code'],
                           user=current_user)