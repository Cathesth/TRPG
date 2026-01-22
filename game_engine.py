import random
import json
import logging
import os
import re
import difflib
import yaml
from typing import TypedDict, List, Dict, Any, Optional, Generator
from langgraph.graph import StateGraph, END
from llm_factory import LLMFactory
from dotenv import load_dotenv
from core.state import WorldState

# [NEW] 토큰 추적 및 과금 처리를 위한 임포트
from langchain_community.callbacks import get_openai_callback
from services.user_service import UserService

# =============================================================================
# [NEW] MinIO 이미지 URL 생성 유틸리티
# =============================================================================
def get_minio_url(category: str, filename: str) -> str:
    """
    MinIO 이미지 URL 생성
    category: backgrounds, npcs, enemies, items
    filename: 이미지 파일명 (확장자 제외 시 .png 자동 추가)
    """
    if not filename:
        return ""

    minio_endpoint = os.getenv("MINIO_ENDPOINT")
    minio_bucket = os.getenv("MINIO_BUCKET", "trpg-assets")
    minio_use_ssl = os.getenv("MINIO_USE_SSL", "false").lower() == "true"

    # [SAFETY] MINIO_ENDPOINT가 없거나 내부용(internal/localhost)인 경우 Railway Public Domain 확인
    if not minio_endpoint or "internal" in minio_endpoint or "localhost" in minio_endpoint:
        railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN")
        if railway_domain:
            minio_endpoint = railway_domain
            # Railway Public Domain은 HTTPS 기본
            minio_use_ssl = True
            logger.info(f"🔧 [MINIO] Used RAILWAY_PUBLIC_DOMAIN fallback: {minio_endpoint}")

    # 여전히 없으면 기본값
    if not minio_endpoint:
        minio_endpoint = "localhost:9000"

    protocol = "https" if minio_use_ssl else "http"

    # [FIX] 이미 URL 형식이면 프록시 처리 또는 도메인 치환
    if str(filename).startswith("http://") or str(filename).startswith("https://"):
        try:
            from urllib.parse import urlparse
            parsed = urlparse(filename)
            
            # 내부망 도메인의 경우 (bucket.railway.internal 등)
            if "internal" in parsed.netloc or "localhost" in parsed.netloc:
                # 시나리오 JSON의 URL이 이미 완전한 경로를 가지고 있다면
                # app.py의 프록시 라우트(/trpg-assets/)를 타도록 경로만 추출해서 반환
                # 예: http://internal:9000/trpg-assets/ai-images/... -> /trpg-assets/ai-images/...
                
                # 경로가 이미 /trpg-assets/를 포함하고 있다면 해당 경로를 그대로 사용 (상대 경로)
                # 브라우저가 현재 도메인 + 이 경로로 요청 -> app.py가 받음 -> MinIO 프록시
                if parsed.path.startswith("/trpg-assets"):
                    return parsed.path
                
                # 만약 /ai-images/로 시작한다면 /trpg-assets/를 붙여줌 (시나리오 데이터별 상이함 대응)
                if parsed.path.startswith("/ai-images"):
                    return f"/trpg-assets{parsed.path}"
                
                # 그 외의 경우 (bucket 이름이 경로에 포함된 경우 등)
                return f"/trpg-assets{parsed.path}"

            # 외부 도메인은 그대로 사용
            return filename
        except:
            return filename

    # [FIX] 파일명 공백 처리 (언더바 치환) & 소문자 변환 (S3/MinIO 호환성)
    filename = str(filename).strip().replace(" ", "_")
    # URL이 아닌 파일명의 경우 소문자로 변환하여 매칭 확률 높임
    if '.' in filename:
        filename = filename.lower()

    # [FIX] 확장자 방어적 추가 (png, jpg, jpeg, webp, gif 지원)
    if not any(filename.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.webp', '.gif']):
        filename = f"{filename.lower()}.png"

    # 파일명 URL 인코딩 (한글 등 특수문자 처리)
    from urllib.parse import quote
    encoded_filename = quote(filename, safe='')

    return f"{protocol}://{minio_endpoint}/{minio_bucket}/{category}/{encoded_filename}"

load_dotenv()

logger = logging.getLogger(__name__)

# [최적화] 시나리오 데이터 캐시
_scenario_cache: Dict[int, Dict[str, Any]] = {}


def get_scenario_by_id(scenario_id: int) -> Dict[str, Any]:
    """
    시나리오 ID로 데이터 조회 (캐싱)
    PlayerState에서 시나리오 전체 데이터를 제거하고 필요 시 이 함수로 조회
    """
    if scenario_id in _scenario_cache:
        return _scenario_cache[scenario_id]

    # DB에서 조회
    from models import SessionLocal, Scenario

    db = SessionLocal()
    try:
        scenario = db.query(Scenario).filter(Scenario.id == scenario_id).first()
        if scenario:
            scenario_data = scenario.data

            # [Fix] 중첩된 scenario 구조 처리
            if 'scenario' in scenario_data and isinstance(scenario_data['scenario'], dict):
                scenario_data = scenario_data['scenario']

            # [Fix] 필수 키가 없으면 기본값 설정
            if 'scenes' not in scenario_data:
                scenario_data['scenes'] = []
            if 'endings' not in scenario_data:
                scenario_data['endings'] = []

            _scenario_cache[scenario_id] = scenario_data
            return scenario_data
        else:
            logger.error(f"❌ Scenario not found: {scenario_id}")
            return {'scenes': [], 'endings': []}
    except Exception as e:
        logger.error(f"❌ Failed to load scenario {scenario_id}: {e}")
        return {'scenes': [], 'endings': []}
    finally:
        db.close()


# =============================================================================
# [NEW] Cache Management
# =============================================================================

def invalidate_scenario_cache(scenario_id: str):
    """
    시나리오 캐시 무효화 - 데이터 일관성 보장
    """
    if scenario_id in _scenario_cache:
        del _scenario_cache[scenario_id]
        logger.info(f"🗑️ [CACHE] Scenario cache invalidated: {scenario_id}")

def refresh_scenario_cache(scenario_id: str):
    """
    시나리오 캐시 새로고침 - DB에서 최신 데이터 로드
    """
    invalidate_scenario_cache(scenario_id)
    return get_scenario_by_id(scenario_id)


# [최적화] 프롬프트 캐시 (YAML 파일에서 한 번만 로드)
_prompt_cache: Dict[str, Any] = {}


def load_player_prompts() -> Dict[str, Any]:
    """플레이어 프롬프트 YAML 파일 로드 (캐싱)"""
    if 'player' not in _prompt_cache:
        prompt_path = os.path.join(os.path.dirname(__file__), 'config', 'prompt_player.yaml')
        try:
            with open(prompt_path, 'r', encoding='utf-8') as f:
                _prompt_cache['player'] = yaml.safe_load(f)
            logger.info(f"📄 [PROMPT] Loaded player prompts from {prompt_path}")
        except Exception as e:
            logger.error(f"Failed to load player prompts: {e}")
            _prompt_cache['player'] = {}
    return _prompt_cache['player']


# [최적화] LLM 인스턴스 캐시 (모델별로 재사용)
_llm_cache: Dict[str, Any] = {}
_llm_streaming_cache: Dict[str, Any] = {}


def get_cached_llm(api_key: str, model_name: str, streaming: bool = False):
    """LLM 인스턴스 캐싱으로 재생성 비용 절감"""
    cache = _llm_streaming_cache if streaming else _llm_cache
    cache_key = f"{model_name}_{streaming}"

    if cache_key not in cache:
        cache[cache_key] = LLMFactory.get_llm(
            api_key=api_key,
            model_name=model_name,
            streaming=streaming
        )
        logger.info(f"🔧 [LLM CACHE] Created new instance: {model_name} (streaming={streaming})")

    return cache[cache_key]


class PlayerState(TypedDict):
    scenario_id: int  # [경량화] 시나리오 전체 대신 ID만 저장
    current_scene_id: str
    previous_scene_id: str
    player_vars: Dict[str, Any]
    history: List[str]
    last_user_choice_idx: int
    last_user_input: str
    parsed_intent: str
    system_message: str
    npc_output: str
    narrator_output: str
    critic_feedback: str
    retry_count: int
    chat_log_html: str
    near_miss_trigger: str  # [필수] Near Miss 저장용
    model: str  # [추가] 사용 중인 LLM 모델
    stuck_count: int  # [추가] 정체 상태 카운터 (장면 전환 실패 횟수)
    _internal_flags: Dict[str, Any]  # [추가] 내부 플래그 (UI에 노출 안 됨)
    world_state: Dict[str, Any]  # [추가] WorldState 스냅샷
    is_game_start: bool  # [추가] 게임 시작 여부 플래그
    target_npc: str  # [추가] 공격 대상 NPC 이름
    user_id: Optional[str]  # [추가] 토큰 과금을 위한 유저 ID


def normalize_text(text: str) -> str:
    """텍스트 정규화 (공백 제거, 소문자)"""
    return text.lower().replace(" ", "")


def format_player_status(scenario: Dict[str, Any], player_vars: Dict[str, Any] = None) -> str:
    """
    플레이어 현재 상태를 포맷팅 (인벤토리 포함)
    player_vars가 제공되면 실제 플레이어 상태를 사용, 없으면 초기 상태 사용
    """
    if player_vars:
        # 실제 플레이어 상태 사용
        current_state = player_vars
    else:
        # 초기 상태 구성
        initial_state = {}

        # 1. variables 필드에서 초기 상태 구성
        if 'variables' in scenario and isinstance(scenario['variables'], list):
            for var in scenario['variables']:
                if isinstance(var, dict) and 'name' in var and 'initial_value' in var:
                    var_name = var['name'].lower()
                    initial_state[var_name] = var['initial_value']

        # 2. initial_state 필드도 확인 (하위 호환성)
        if 'initial_state' in scenario:
            initial_state.update(scenario['initial_state'])

        current_state = initial_state

    # 상태가 비어있으면 빈 문자열 반환
    if not current_state:
        return "초기 상태 없음"

    status_lines = []
    inventory = current_state.get('inventory', [])

    for key, value in current_state.items():
        if key == 'inventory':
            continue
        if isinstance(value, (int, float)):
            status_lines.append(f"- {key}: {value}")
        elif isinstance(value, str):
            status_lines.append(f"- {key}: {value}")

    # 인벤토리는 마지막에 추가 (강조)
    if inventory and isinstance(inventory, list):
        # [NEW] 아이템 이미지를 HTML 태그로 포함
        items_html_list = []
        for item in inventory:
            item_name = str(item)
            item_img_url = ""

            # 🛠️ [Improvement] 시나리오 데이터에서 아이템 이미지 검색 (우선순위 1)
            # scenario 구조: items 리스트가 최상위에 있거나 raw_graph 안에 있을 수 있음
            found_image = False
            
            # 1. raw_graph > items 검색
            if 'raw_graph' in scenario and 'items' in scenario['raw_graph']:
                for item_def in scenario['raw_graph']['items']:
                    if item_def.get('name') == item_name and item_def.get('image'):
                        # [FIX] 내부 URL 치환을 위해 get_minio_url 호출
                        item_img_url = get_minio_url('items', item_def['image'])
                        found_image = True
                        break
            
            # 2. scenario > items 검색 (구조에 따라 다름)
            if not found_image and 'items' in scenario:
                for item_def in scenario['items']:
                    if isinstance(item_def, dict) and item_def.get('name') == item_name and item_def.get('image'):
                        # [FIX] 내부 URL 치환을 위해 get_minio_url 호출
                        item_img_url = get_minio_url('items', item_def['image'])
                        found_image = True
                        break

            # 3. MinIO URL 자동 생성 (폴백)
            if not found_image:
                item_img_url = get_minio_url('items', item_name)

            # 아이템 아이콘 + 이름 형태로 구성
            items_html_list.append(
                f'<span class="inline-flex items-center gap-1 px-2 py-1 bg-gray-800/50 rounded border border-gray-600">'
                f'<img src="{item_img_url}" class="w-5 h-5 rounded" onerror="this.style.display=\'none\'">'
                f'<span class="text-sm">{item_name}</span>'
                f'</span>'
            )

        items_html = ' '.join(items_html_list)
        status_lines.append(f"- 🎒 소지품 (인벤토리): {items_html}")
    else:
        status_lines.append(f"- 🎒 소지품 (인벤토리): [비어 있음]")

    return '\n  '.join(status_lines)


# --- Nodes ---

# 부정적 결말로 가는 transition 필터링 함수
def filter_negative_transitions(transitions: list, scenario: dict) -> list:
    """
    힌트 생성 시 부정적인 결말(ending, 패배, 죽음 등)로 가는 경로를 제외
    """
    negative_keywords = ['패배', '죽음', 'death', 'defeat', 'game_over', 'bad_end', '실패', '사망', '처치', '엔딩', 'ending', '종료',
                         '끝', 'die', 'kill', 'dead', 'lose', 'lost']
    endings = {e['ending_id'].lower(): e for e in scenario.get('endings', [])}

    filtered = []
    for trans in transitions:
        target = trans.get('target_scene_id', '').lower()
        trigger = trans.get('trigger', '').lower()

        # 엔딩으로 가는 transition은 모두 제외 (긍/부정 무관)
        if target.startswith('ending') or target in endings:
            continue

        # trigger 자체에 부정적 키워드가 있으면 제외
        if any(kw in trigger for kw in negative_keywords):
            continue

        filtered.append(trans)

    return filtered if filtered else []  # 적합한 게 없으면 빈 리스트 반환


# 서사적 내레이션 힌트 (관찰자 시점) - YAML에서 로드
def get_narrative_hint_messages() -> List[str]:
    prompts = load_player_prompts()
    return prompts.get('narrative_hint_messages', [
        "주변의 공기가 긴장감으로 가득 차 있습니다. 무언가 눈에 띄는 것이 있을지도 모릅니다."
    ])


# 전투 씬 방어 행동 관련 내레이션 - YAML에서 로드
def get_battle_defensive_messages() -> List[str]:
    prompts = load_player_prompts()
    return prompts.get('battle_defensive_messages', [
        "당신은 몸을 낮추고 방어 자세를 취했습니다."
    ])


# Near Miss 상황용 서사적 힌트 - YAML에서 로드
def get_near_miss_narrative_hints() -> List[str]:
    prompts = load_player_prompts()
    return prompts.get('near_miss_narrative_hints', [
        "거의 통할 뻔했습니다. 무언가 반응이 있었습니다."
    ])


# 전투 씬 공격 행동 관련 내레이션 - YAML에서 로드
def get_battle_attack_messages() -> List[str]:
    prompts = load_player_prompts()
    return prompts.get('battle_attack_messages', [
        "당신의 공격이 적에게 닿았지만, 치명상을 입히지는 못했습니다."
    ])


# 전투 씬 교착 상태 내레이션 - YAML에서 로드
def get_battle_stalemate_messages() -> List[str]:
    prompts = load_player_prompts()
    return prompts.get('battle_stalemate_messages', [
        "치열한 공방이 이어집니다. 적도 당신도 결정타를 내지 못하고 있습니다."
    ])


def get_npc_weakness_hint(scenario: Dict[str, Any], enemy_names: List[str]) -> str:
    """
    NPC 데이터에서 약점을 찾아 서사적 힌트로 변환
    절대 직접적으로 '약점을 써라'라고 하지 않고, 환경 묘사로 힌트 제공
    """
    prompts = load_player_prompts()
    weakness_hints = prompts.get('weakness_hints', {})
    npcs = scenario.get('npcs', [])

    # 🔴 [CRITICAL] enemy_names 리스트 정규화: 딕셔너리면 name 필드 추출
    normalized_enemies = [e.get('name') if isinstance(e, dict) else e for e in enemy_names]

    for npc in npcs:
        npc_name = npc.get('name', '')
        if npc_name in normalized_enemies:
            weakness = npc.get('weakness', npc.get('약점', ''))
            if weakness:
                weakness_lower = weakness.lower()

                if '소금' in weakness_lower or 'salt' in weakness_lower or '염' in weakness_lower:
                    hints = weakness_hints.get('salt', ["바닥에 쏟아진 짠물이 발밑에서 번들거립니다."])
                    return random.choice(hints)
                elif '빛' in weakness_lower or 'light' in weakness_lower:
                    hints = weakness_hints.get('light', ["천장의 조명이 깜빡이며 강렬한 빛을 내뿜습니다."])
                    return random.choice(hints)
                elif '불' in weakness_lower or 'fire' in weakness_lower or '화염' in weakness_lower:
                    hints = weakness_hints.get('fire', ["근처에 라이터가 떨어져 있습니다."])
                    return random.choice(hints)
                elif '물' in weakness_lower or 'water' in weakness_lower:
                    hints = weakness_hints.get('water', ["파열된 수도관에서 물이 뿜어져 나오고 있습니다."])
                    return random.choice(hints)
                elif '전기' in weakness_lower or 'electric' in weakness_lower:
                    hints = weakness_hints.get('electric', ["노출된 전선이 스파크를 일으키고 있습니다."])
                    return random.choice(hints)
                else:
                    default_hint = weakness_hints.get('default', "주변을 둘러보니, {weakness}과(와) 관련된 무언가가 눈에 들어옵니다.")
                    return default_hint.format(weakness=weakness)

    return ""


def check_victory_condition(user_input: str, scenario: Dict[str, Any], curr_scene: Dict[str, Any]) -> bool:
    """
    확실한 승리 조건이 만족되었는지 검사
    단순 '공격'만으로는 승리하지 않음 - 약점 활용이나 특수 조건 필요
    """
    transitions = curr_scene.get('transitions', [])
    user_lower = user_input.lower()

    # 적 정보 가져오기
    enemy_names = curr_scene.get('enemies', [])
    npcs = scenario.get('npcs', [])

    for npc in npcs:
        if npc.get('name', '') in enemy_names:
            weakness = npc.get('weakness', npc.get('약점', '')).lower()
            if weakness:
                # 약점이 입력에 포함되어 있으면 승리 조건 충족
                weakness_keywords = weakness.replace(',', ' ').replace('/', ' ').split()
                for kw in weakness_keywords:
                    if kw and len(kw) >= 2 and kw in user_lower:
                        return True

    # transition에 명시된 승리 trigger와 정확히 일치하는지 확인
    for trans in transitions:
        trigger = trans.get('trigger', '').lower()
        target = trans.get('target_scene_id', '').lower()

        # 긍정적 엔딩(승리)으로 가는 경로인지 확인
        if 'victory' in target or 'win' in target or '승리' in trigger:
            # 유사도가 매우 높을 때만 승리 인정 (0.8 이상)
            norm_input = normalize_text(user_input)
            norm_trigger = normalize_text(trigger)
            similarity = difflib.SequenceMatcher(None, norm_input, norm_trigger).ratio()
            if similarity >= 0.8:
                return True

    return False


def intent_parser_node(state: PlayerState):
    """
    [계층형 파서로 업그레이드]
    우선순위:
    1. 하드코딩 필터 (따옴표, 완전 일치만)
    2. LLM 의도 분류 (intent_classifier)
    3. Fast-Track 폴백
    """

    # ✅ 작업 1: 상태 초기화 (중요: 이전 턴의 출력 필드를 무조건 제거)
    state['near_miss_trigger'] = ''
    state['npc_output'] = ''
    state['narrator_output'] = ''
    state['system_message'] = ''
    state['critic_feedback'] = ''
    logger.info("🧹 [CLEANUP] Output fields cleared for new turn")

    # 🔍 [SESSION ISOLATION] WorldState 로컬 인스턴스 생성
    session_id = state.get('scenario_id', 'unknown')
    wsm = WorldState()
    ws_dict = state.get('world_state') or {}
    if ws_dict:
        wsm.from_dict(ws_dict)
    logger.info(f"🔍 [SESSION ISOLATION] Created local WorldState instance for session: {session_id}")

    # ✅ 작업 2: PlayerState의 current_scene_id를 절대적 진실(Source of Truth)로 믿고, world_state.location을 동기화
    curr_scene_id_from_state = state.get('current_scene_id', '')
    ws_location = wsm.location

    # ✅ 작업 2: 위치가 다를 경우, state['current_scene_id']를 기준으로 world_state.location 강제 업데이트
    if curr_scene_id_from_state and ws_location != curr_scene_id_from_state:
        logger.warning(
            f"⚠️ [INTENT_PARSER] Location regression detected! "
            f"state.current_scene_id: '{curr_scene_id_from_state}' (TRUTH) vs world_state.location: '{ws_location}' (OUTDATED)"
        )
        logger.info(
            f"🔧 [LOCATION SYNC] Forcing world_state.location = '{curr_scene_id_from_state}' (state.current_scene_id is Source of Truth)")
        wsm.location = curr_scene_id_from_state
    elif not curr_scene_id_from_state and ws_location:
        # current_scene_id가 비어있으면 world_state.location으로 복원
        logger.info(f"🔄 [INTENT_PARSER] Restored scene from world_state.location: {ws_location}")
        state['current_scene_id'] = ws_location
        curr_scene_id_from_state = ws_location
    elif not curr_scene_id_from_state and not ws_location:
        # ✅ 작업 2: 둘 다 비어있을 때만 기본값 설정 (Scene-1 회귀 방지)
        logger.warning(
            "⚠️ [INTENT_PARSER] Both current_scene_id and world_state.location are empty, using 'prologue' as default")
        curr_scene_id_from_state = 'prologue'
        state['current_scene_id'] = curr_scene_id_from_state
        wsm.location = curr_scene_id_from_state

    # previous_scene_id 설정
    if curr_scene_id_from_state:
        state['previous_scene_id'] = curr_scene_id_from_state

    user_input = state.get('last_user_input', '').strip()

    # ✅ 정합성 로그
    logger.info(
        f"🟢 [INTENT_PARSER START] USER INPUT: '{user_input}' | Scene: '{curr_scene_id_from_state}' (from state.current_scene_id - SOURCE OF TRUTH)")

    # ✅ 노드 종료 전 world_state 저장
    state['world_state'] = wsm.to_dict()

    if not user_input:
        state['parsed_intent'] = 'chat'
        state['system_message'] = "행동을 입력해주세요."
        return state

    # 시스템적 선택 처리
    if state.get('last_user_choice_idx', -1) != -1:
        state['parsed_intent'] = 'transition'
        return state

    scenario_id = state['scenario_id']
    curr_scene_id = state['current_scene_id']
    scenes = {s['scene_id']: s for s in get_scenario_by_id(scenario_id).get('scenes', [])}

    curr_scene = scenes.get(curr_scene_id)
    if not curr_scene:
        state['parsed_intent'] = 'chat'
        return state

    # 엔딩 체크
    endings = {e['ending_id']: e for e in get_scenario_by_id(scenario_id).get('endings', [])}
    if curr_scene_id in endings:
        state['parsed_intent'] = 'ending'
        return state

    transitions = curr_scene.get('transitions', [])
    scene_type = curr_scene.get('type', 'normal')
    scene_title = curr_scene.get('title', 'Untitled')
    npc_names = curr_scene.get('npcs', [])
    enemy_names = curr_scene.get('enemies', [])

    # =============================================================================
    # [작업 1] 하드코딩 기반 고우선순위 필터링 (최소화)
    # =============================================================================

    logger.info(
        f"🎯 [HARDCODE FILTER START] Filtering based on scene: '{curr_scene_id}' | Total transitions: {len(transitions)}")

    # 1-1. 따옴표 감지 -> 무조건 'chat' (대사/대화)
    if '"' in user_input or "'" in user_input or '"' in user_input or '"' in user_input or ''' in user_input or ''' in user_input:
        logger.info(f"🎤 [HARDCODE FILTER] 따옴표 감지 -> 'chat' 강제 분류 (scene: '{curr_scene_id}')")
        state['parsed_intent'] = 'chat'
        return state

    # 1-2. transitions와 100% 완전 일치 -> 즉시 'transition'
    norm_input = normalize_text(user_input)
    for idx, trans in enumerate(transitions):
        trigger = trans.get('trigger', '').strip()
        if not trigger:
            continue
        norm_trigger = normalize_text(trigger)

        if norm_input == norm_trigger:
            logger.info(f"🎯 [HARDCODE FILTER] 100% 일치 감지 -> '{trigger}' (idx={idx}, scene: '{curr_scene_id}')")
            state['last_user_choice_idx'] = idx
            state['parsed_intent'] = 'transition'
            return state

    logger.info(
        f"🎯 [HARDCODE FILTER END] No hardcode match found in scene '{curr_scene_id}', proceeding to LLM classifier")

    # =============================================================================
    # [작업 2] LLM을 통한 의도 분류 (2단계 API 호출)
    # =============================================================================

    try:
        # transitions 목록을 문자열로 포맷팅
        transitions_list = ""
        if transitions:
            transitions_list += "📋 **[AVAILABLE ACTIONS - 이것들이 다음 장면으로 이동 가능한 정답입니다]**\n"
            transitions_list += "다음 키워드들 중 하나와 유사한 입력이 들어오면 transition으로 분류하세요:\n\n"
            for idx, trans in enumerate(transitions):
                trigger = trans.get('trigger', '').strip()
                target = trans.get('target_scene_id', '')
                transitions_list += f"  {idx}. 트리거: \"{trigger}\" → {target}\n"
            transitions_list += "\n⚠️ 유저 입력이 위 트리거와 70% 이상 의미적으로 유사하면 transition으로 분류하세요."
        else:
            transitions_list = "없음 (이동 불가)"

        # YAML에서 intent_classifier 프롬프트 로드
        prompts = load_player_prompts()
        intent_classifier_template = prompts.get('intent_classifier', '')

        if not intent_classifier_template:
            logger.warning("⚠️ intent_classifier prompt not found, falling back to fast-track")
            return _fast_track_intent_parser(state, user_input, curr_scene, get_scenario_by_id(scenario_id), endings)

        # 프롬프트 생성
        scenario = get_scenario_by_id(scenario_id)
        player_status = format_player_status(scenario, state.get('player_vars', {}))

        intent_prompt = intent_classifier_template.format(
            player_status=player_status,
            scene_title=scene_title,
            scene_type=scene_type,
            npc_list=', '.join(npc_names) if npc_names else '없음',
            enemy_list=', '.join(enemy_names) if enemy_names else '없음',
            transitions_list=transitions_list,
            user_input=user_input
        )

        # LLM 호출 (non-streaming)
        api_key = os.getenv("OPENROUTER_API_KEY")
        model_name = state.get('model', 'openai/tngtech/deepseek-r1t2-chimera:free')
        llm = get_cached_llm(api_key=api_key, model_name=model_name, streaming=False)

        # [TOKEN] invoke 호출 시 상위 context manager가 있으면 토큰이 추적됨
        response = llm.invoke(intent_prompt).content.strip()
        logger.info(f"🤖 [INTENT CLASSIFIER] Raw response: {response}")

        # JSON 파싱 시도
        json_match = re.search(r'\{.*}', response, re.DOTALL)
        if json_match:
            json_str = json_match.group(0)
            intent_result = json.loads(json_str)

            intent_type = intent_result.get('intent', 'chat')
            transition_index = intent_result.get('transition_index', -1)
            confidence = intent_result.get('confidence', 0.0)
            reasoning = intent_result.get('reasoning', '')
            target_npc = intent_result.get('target_npc', None)
            item_name = intent_result.get('item_name', None)  # 📦 [NEW] LLM이 추출한 아이템 이름

            logger.info(f"🎯 [INTENT] Type: {intent_type}, Confidence: {confidence:.2f}, Reasoning: {reasoning}")

            # 📦 [NEW] item_name 저장 (LLM 데이터 캡처)
            if item_name:
                if '_internal_flags' not in state:
                    state['_internal_flags'] = {}
                state['_internal_flags']['item_name'] = item_name
                logger.info(f"📦 [ITEM SYSTEM] Item name extracted by LLM: '{item_name}'")
            else:
                # item_name이 없으면 초기화
                if '_internal_flags' in state:
                    state['_internal_flags'].pop('item_name', None)

            # target_npc 저장
            if target_npc:
                state['target_npc'] = target_npc
                logger.info(f"🎯 [TARGET] Target NPC extracted: '{target_npc}'")
            else:
                state['target_npc'] = ''

            # 의도에 따른 처리
            if intent_type == 'transition' and 0 <= transition_index < len(transitions):
                # 전투 씬에서 엔딩으로 가는 transition은 승리 조건 체크
                target_trans = transitions[transition_index]
                target = target_trans.get('target_scene_id', '').lower()
                is_ending_transition = target.startswith('ending') or target in endings

                if scene_type == 'battle' and is_ending_transition:
                    if not check_victory_condition(user_input, scenario, curr_scene):
                        logger.info(f"⚔️ [BATTLE] Transition blocked - victory condition not met")
                        state['parsed_intent'] = 'attack'
                        state['_internal_flags'] = state.get('_internal_flags', {})
                        state['_internal_flags']['battle_attack'] = True
                        return state

                state['last_user_choice_idx'] = transition_index
                state['parsed_intent'] = 'transition'
                return state

            elif intent_type == 'investigate':
                state['parsed_intent'] = 'investigate'
                return state

            elif intent_type == 'attack':
                state['parsed_intent'] = 'attack'
                if target_npc:
                    state['target_npc'] = target_npc
                    logger.info(f"🎯 [ATTACK] Target NPC saved: '{target_npc}'")
                else:
                    # target_npc 추출 보강 - 자동 타겟팅
                    npc_list = npc_names + enemy_names

                    for npc_name in npc_list:
                        if npc_name in user_input or npc_name.replace(' ', '').lower() in user_input.lower().replace(
                                ' ', ''):
                            state['target_npc'] = npc_name
                            logger.info(f"🎯 [ATTACK] Target extracted from input: '{npc_name}'")
                            break

                    if not state.get('target_npc'):
                        wsm_temp = WorldState()
                        if state.get('world_state'):
                            wsm_temp.from_dict(state['world_state'])

                        for word in user_input.split():
                            potential_target = wsm_temp.find_npc_key(word)
                            if potential_target and potential_target in npc_list:
                                state['target_npc'] = potential_target
                                logger.info(f"🎯 [ATTACK] Target found via find_npc_key: '{potential_target}'")
                                break

                    if not state.get('target_npc') and npc_list:
                        if enemy_names:
                            state['target_npc'] = enemy_names[0]
                            logger.info(f"🎯 [ATTACK] Auto-targeting first enemy: '{enemy_names[0]}'")
                        elif npc_names:
                            state['target_npc'] = npc_names[0]
                            logger.info(f"🎯 [ATTACK] Auto-targeting first NPC: '{npc_names[0]}'")

                    if not state.get('target_npc'):
                        state['target_npc'] = ''
                        logger.warning(f"⚠️ [ATTACK] No target found in input: '{user_input}'")

                logger.info(f"⚔️ [INTENT] Attack intent preserved (scene_type: {scene_type})")
                return state

            elif intent_type == 'defend':
                state['parsed_intent'] = 'defend'
                return state

            elif intent_type == 'item_action':
                # 📦 [NEW] item_action 의도 처리
                state['parsed_intent'] = 'item_action'
                logger.info(f"📦 [INTENT] Item action detected, item_name: '{item_name}'")
                return state

            else:  # chat
                state['parsed_intent'] = 'chat'
                return state

        else:
            logger.warning("⚠️ Failed to parse JSON from intent classifier, falling back to fast-track")
            return _fast_track_intent_parser(state, user_input, curr_scene, scenario, endings)

    except Exception as e:
        logger.error(f"❌ [INTENT CLASSIFIER] Error: {e}, falling back to fast-track")
        return _fast_track_intent_parser(state, user_input, curr_scene, get_scenario_by_id(scenario_id), endings)


def _fast_track_intent_parser(state: PlayerState, user_input: str, curr_scene: Dict, scenario: Dict, endings: Dict):
    """
    기존 Fast-Track 의도 파서 (폴백용)
    ✅ [작업 3] Near Miss 로직 강화 - 0.4~0.6 구간에서 trigger 전체 문구 저장
    """
    norm_input = normalize_text(user_input)
    transitions = curr_scene.get('transitions', [])
    scene_type = curr_scene.get('type', 'normal')

    if not transitions:
        state['parsed_intent'] = 'chat'
        return state

    # 공격 행동 감지
    attack_keywords = ['공격', '때리', '치', '베', '찌르', '쏘', '던지', '싸우', 'attack', 'hit', 'strike', 'fight', 'kill', '처치',
                       '죽이', '무찌']
    is_attack_action = any(kw in user_input.lower() for kw in attack_keywords)

    if scene_type == 'battle' and is_attack_action:
        if not check_victory_condition(user_input, scenario, curr_scene):
            logger.info(f"⚔️ [BATTLE] Attack detected but victory condition not met. Continuing battle.")
            state['parsed_intent'] = 'attack'
            state['_internal_flags'] = state.get('_internal_flags', {})
            state['_internal_flags']['battle_attack'] = True
            return state

    # Fast-Track 매칭
    best_idx = -1
    highest_ratio = 0.0
    best_trigger_text = ""

    for idx, trans in enumerate(transitions):
        trigger = trans.get('trigger', '').strip()
        if not trigger: continue
        norm_trigger = normalize_text(trigger)
        target = trans.get('target_scene_id', '').lower()
        is_ending_transition = target.startswith('ending') or target in endings

        # 완전 포함 관계
        if norm_input in norm_trigger or norm_trigger in norm_input:
            if len(norm_input) >= 2:
                if scene_type == 'battle' and is_ending_transition:
                    if not check_victory_condition(user_input, scenario, curr_scene):
                        continue

                logger.info(f"⚡ [FAST-TRACK] Direct Match: '{user_input}' matched '{trigger}'")
                state['last_user_choice_idx'] = idx
                state['parsed_intent'] = 'transition'
                return state

        # 유사도 계산
        similarity = difflib.SequenceMatcher(None, norm_input, norm_trigger).ratio()

        if scene_type == 'battle' and is_ending_transition:
            if similarity < 0.8:
                continue

        if similarity > highest_ratio:
            highest_ratio = similarity
            best_idx = idx
            best_trigger_text = trigger

    # 0.6 이상: 성공
    if highest_ratio >= 0.6:
        target_trans = transitions[best_idx]
        target = target_trans.get('target_scene_id', '').lower()
        is_ending_transition = target.startswith('ending') or target in endings

        if scene_type == 'battle' and is_ending_transition:
            if not check_victory_condition(user_input, scenario, curr_scene):
                logger.info(f"⚔️ [BATTLE] Fuzzy match to ending blocked - victory condition not met")
                state['parsed_intent'] = 'attack'
                state['_internal_flags'] = state.get('_internal_flags', {})
                state['_internal_flags']['battle_attack'] = True
                return state

        logger.info(f"⚡ [FAST-TRACK] Fuzzy Match ({highest_ratio:.2f}): '{user_input}' -> '{best_trigger_text}'")
        state['last_user_choice_idx'] = best_idx
        state['parsed_intent'] = 'transition'
        return

    # ✅ [작업 3] 0.4 ~ 0.59: Near Miss - 가장 가까운 트리거 전체 문구 저장
    elif highest_ratio >= 0.4:
        logger.info(
            f"⚠️ [NEAR MISS] Similarity: {highest_ratio:.2f} | User: '{user_input}' vs Trigger: '{best_trigger_text}'")
        # 트리거 전체 문구를 저장하여 나레이션 노드에서 힌트 제공
        state['near_miss_trigger'] = best_trigger_text
        state['parsed_intent'] = 'chat'
        logger.info(f"💡 [HINT] near_miss_trigger set to: '{best_trigger_text}' (나레이션에서 힌트 제공 예정)")
        return state

    # 매칭 실패 -> 일반 채팅/힌트
    state['parsed_intent'] = 'chat'
    return state


def rule_node(state: PlayerState):
    """규칙 엔진 (이동 및 상태 변경) - WorldState 통합"""
    # ✅ [FIX] 변수 미정의 해결: user_input을 최상단에 선언
    user_input = state.get('last_user_input', '').strip()

    idx = state['last_user_choice_idx']
    scenario_id = state['scenario_id']
    curr_scene_id = state['current_scene_id']
    prev_scene_id = state.get('previous_scene_id')

    all_scenes = {s['scene_id']: s for s in get_scenario_by_id(scenario_id)['scenes']}
    all_endings = {e['ending_id']: e for e in get_scenario_by_id(scenario_id).get('endings', [])}

    sys_msg = []
    curr_scene = all_scenes.get(curr_scene_id)
    transitions = curr_scene.get('transitions', []) if curr_scene else []

    # WorldState 인스턴스 가져오기 및 복원
    world_state = WorldState()

    # [FIX] 기존 world_state가 있으면 복원
    if 'world_state' in state and state['world_state']:
        world_state.from_dict(state['world_state'])
    else:
        # 처음 생성하는 경우 시나리오로 초기화
        scenario = get_scenario_by_id(scenario_id)
        world_state.initialize_from_scenario(scenario)

    # ✅ [작업 1-1] 턴 시작 시점에 실제 현재 위치를 명시적으로 캡처 (이것이 진실!)
    actual_current_location = world_state.location
    logger.info(
        f"📍 [RULE_NODE START] Captured actual_current_location: '{actual_current_location}' (from world_state.location)")

    # ✅ [작업 1-3] 턴 시작 시 위치 정보 검증 - world_state.location과 state['current_scene_id'] 일치 확인
    if state['current_scene_id'] != actual_current_location:
        logger.warning(
            f"⚠️ [LOCATION MISMATCH] state['current_scene_id']: '{state['current_scene_id']}' "
            f"!= world_state.location: '{actual_current_location}'"
        )
        logger.info(f"🔧 [LOCATION FIX] Forcing state['current_scene_id'] = '{actual_current_location}'")
        state['current_scene_id'] = actual_current_location
        curr_scene_id = actual_current_location
        curr_scene = all_scenes.get(curr_scene_id)
        transitions = curr_scene.get('transitions', []) if curr_scene else []

    # ✅ [작업 1] 턴 카운트 증가 로직을 함수 시작 부분으로 이동
    # 게임 시작이 아닐 때만 턴 증가 (Game Started는 Turn 1을 가져감)
    is_game_start = state.get('is_game_start', False)
    if not is_game_start:
        world_state.increment_turn()
        logger.info(f"⏱️ [TURN] Turn count increased to {world_state.turn_count} at rule_node start")
    else:
        logger.info(f"⏱️ [TURN] Game start - turn count not increased (current: {world_state.turn_count})")

    # ✅ 작업 2: stuck_count 초기화 (state에 없으면 0으로 설정)
    if 'stuck_count' not in state:
        state['stuck_count'] = 0
        logger.info(f"🔧 [STUCK_COUNT] Initialized to 0")

    # user_action 추출 (서사 이벤트 기록용)
    user_action = user_input if user_input else None

    logger.info(
        f"🎬 [APPLY_EFFECTS] Scene before transition: {actual_current_location}, Intent: {state['parsed_intent']}, Transition index: {idx}")

    # ========================================
    # ⚔️ 작업 1 & 3: attack 의도 처리 (전투 로직 주입 + 시체 확인)
    # ========================================
    if state['parsed_intent'] == 'attack':
        logger.info(f"⚔️ [RULE_NODE COMBAT] Attack intent detected in rule_node")

        # (a) target_npc 추출
        target_npc = state.get('target_npc', '')

        # target_npc가 없으면 현재 씬의 NPC/적 목록에서 추출 시도
        if not target_npc:
            npc_list = curr_scene.get('npcs', []) + curr_scene.get('enemies', []) if curr_scene else []

            # user_input에서 NPC 이름 매칭 시도
            for npc_name in npc_list:
                if npc_name in user_input or npc_name.replace(' ', '').lower() in user_input.lower().replace(' ', ''):
                    target_npc = npc_name
                    logger.info(f"🎯 [COMBAT] Target extracted from input in rule_node: '{target_npc}'")
                    break

            # 그래도 못찾으면 world_state.find_npc_key 사용
            if not target_npc and npc_list:
                for word in user_input.split():
                    potential_target = world_state.find_npc_key(word)
                    if potential_target:
                        target_npc = potential_target
                        logger.info(f"🎯 [COMBAT] Target found via find_npc_key in rule_node: '{target_npc}'")
                        break

        # (b) target_npc가 확정되지 않으면 에러 처리
        if not target_npc:
            logger.warning(f"⚠️ [COMBAT] Attack target unclear in rule_node. User input: '{user_input}'")
            sys_msg.append("⚠️ 공격 대상이 불명확합니다.")
            state['system_message'] = " | ".join(sys_msg)
            state['world_state'] = world_state.to_dict()
            return state

        # ========================================
        # 💀 작업 1: 죽은 NPC 확인 - 최상단에서 체크
        # ========================================
        npc_state = world_state.get_npc_state(target_npc)
        if npc_state and npc_state.get('status') == 'dead':
            logger.info(f"💀 [COMBAT] {target_npc} is dead, blocking further attacks")

            # 이미 죽은 NPC 공격 시 메시지
            sys_msg.append(f"⚠️ {target_npc}은(는) 이미 사망했습니다.")
            state['system_message'] = " | ".join(sys_msg)
            state['world_state'] = world_state.to_dict()
            return state

        # (c) 데미지 산정 (random 10~20)
        damage = random.randint(10, 20)
        logger.info(f"🎲 [COMBAT] Damage roll: {damage}")

        # (d) world_state.damage_npc 호출
        combat_result = world_state.damage_npc(target_npc, damage)

        logger.info(f"⚔️ [COMBAT] Result: {combat_result}")

        # ========================================
        # 💰 [전리품 시스템] NPC HP가 0 이하가 되어 status='dead'로 변경된 경우 drop_items 처리
        # ========================================
        npc_state_after = world_state.get_npc_state(target_npc)
        if npc_state_after and npc_state_after.get('status') == 'dead':
            logger.info(f"💀 [LOOT] {target_npc} has died, checking for loot...")

            # 시나리오에서 NPC 데이터 조회
            scenario_data = get_scenario_by_id(scenario_id)
            npcs_data = scenario_data.get('npcs', [])

            # npcs가 딕셔너리 형태일 수도 있으므로 안전하게 처리
            if isinstance(npcs_data, dict):
                npcs_data = list(npcs_data.values())

            # 해당 NPC의 drop_items 확인
            dropped_items = []
            for npc_data in npcs_data:
                if not isinstance(npc_data, dict):
                    continue

                if npc_data.get('name') == target_npc:
                    drop_items_raw = npc_data.get('drop_items', [])

                    # 🔧 [FIX] drop_items가 문자열인 경우 쉼표로 분리
                    if isinstance(drop_items_raw, str):
                        drop_items = [item.strip() for item in drop_items_raw.split(',') if item.strip()]
                        logger.info(f"💰 [LOOT] Parsed drop_items from string: {drop_items}")
                    elif isinstance(drop_items_raw, list):
                        drop_items = drop_items_raw
                    else:
                        drop_items = []

                    if drop_items:
                        # 아이템 드랍 처리
                        for item_name in drop_items:
                            world_state._add_item(item_name)
                            dropped_items.append(item_name)
                            logger.info(f"💰 [LOOT] {target_npc} dropped item: '{item_name}'")

                        # player_vars의 inventory도 동기화 강제
                        state['player_vars']['inventory'] = list(world_state.player["inventory"])
                        logger.info(
                            f"📦 [ITEM SYSTEM] Synced inventory to player_vars after loot: {state['player_vars']['inventory']}")

                        logger.info(f"💰 [LOOT] Total items dropped from {target_npc}: {len(drop_items)}")
                    else:
                        logger.info(f"💰 [LOOT] No items to drop from {target_npc}")
                    break

            # system_message에 전리품 정보 추가
            if dropped_items:
                items_text = ', '.join(dropped_items)
                combat_result += f"\n💰 전투 승리! [{items_text}]을(를) 획득했습니다!"

                # narrative_history에 기록
                world_state.add_narrative_event(f"{target_npc} 처치 후 전리품 [{items_text}] 획득")

        # ========================================
        # 💥 작업 2: 플레이어 HP 동기화 - WorldState의 HP를 player_vars에 강제 동기화
        # ========================================
        world_state_hp = world_state.player.get("hp", 100)
        state['player_vars']['hp'] = world_state_hp
        logger.info(
            f"💾 [HP SYNC] Player HP synced to player_vars: {world_state_hp} (world_state.player['hp'] -> state['player_vars']['hp'])")

        # (f) system_message에 결과 저장
        state['system_message'] = combat_result

        # (g) narrative_history에 기록
        world_state.record_combat_event(f"플레이어가 {target_npc}을(를) 공격: {combat_result}")

        # ========================================
        # 💥 작업 2 & 4: 노드 종료 직전 최종 HP 동기화 강제 + 검증 로그
        # ========================================
        state['player_vars']['hp'] = world_state.player["hp"]
        logger.info(f"💾 [FINAL HP SYNC] Final Player HP sync before save: {world_state.player['hp']}")
        logger.info(f"💾 [DB PRE-SAVE] Final Player HP in state (rule_node): {state['player_vars']['hp']}")

        # ✅ 작업 1: attack 의도 시 stuck_count 증가 (장면 이동 없음)
        old_stuck_count = state.get('stuck_count', 0)
        state['stuck_count'] = old_stuck_count + 1
        world_state.stuck_count = state['stuck_count']
        logger.info(f"📈 [PROGRESS] stuck_count increased: {old_stuck_count} -> {state['stuck_count']} (attack intent)")

        # (h) world_state 갱신
        state['world_state'] = world_state.to_dict()

        # NPC 대사는 생성하지 않음 (공격 결과만 표시)
        state['npc_output'] = ""

        logger.info(f"✅ [COMBAT] Attack processing complete. Damage: {damage}, Target: {target_npc}")

        return state

    # ========================================
    # 🗑️ 아이템 버리기/줍기 의도 처리 (item_action)
    # ========================================
    if state['parsed_intent'] == 'item_action':
        logger.info(f"🎒 [ITEM_ACTION] Item action intent detected in rule_node")

        player_vars = state.get('player_vars', {})
        inventory = player_vars.get('inventory', [])

        # 📦 [NEW] LLM이 추출한 item_name을 최우선으로 사용
        item_name = state.get('_internal_flags', {}).get('item_name', None)

        if not item_name:
            # 폴백: user_input에서 명사 추출 (방어적 처리)
            logger.warning(f"⚠️ [ITEM_ACTION] No item_name from LLM, fallback to user_input parsing")

            # 시나리오의 아이템 목록에서 매칭 시도
            scenario_data = get_scenario_by_id(scenario_id)
            available_items = scenario_data.get('items', [])

            for item_data in available_items:
                if isinstance(item_data, dict):
                    item_candidate = item_data.get('name', '')
                    if item_candidate and item_candidate in user_input:
                        item_name = item_candidate
                        logger.info(f"📦 [ITEM SYSTEM] Item name extracted from user_input: '{item_name}'")
                        break

            # 인벤토리에서도 매칭 시도
            if not item_name:
                for inv_item in inventory:
                    if str(inv_item) in user_input:
                        item_name = inv_item
                        logger.info(f"📦 [ITEM SYSTEM] Item name extracted from inventory: '{item_name}'")
                        break

        if not item_name:
            logger.warning(f"⚠️ [ITEM_ACTION] Failed to extract item_name. User input: '{user_input}'")
            sys_msg.append(f"⚠️ 아이템 이름을 인식할 수 없습니다.")
            state['system_message'] = " | ".join(sys_msg)
            state['world_state'] = world_state.to_dict()
            return state

        logger.info(f"📦 [ITEM SYSTEM] Processing item action for: '{item_name}'")

        # 버리기/줍기 판단 (LLM 추론 활용)
        discard_keywords = ['버리', '버려', '버린', '던져', '던지', '버렸', '폐기', '제거', '내려놓']
        is_discard_action = any(kw in user_input for kw in discard_keywords)

        pickup_keywords = ['줍', '습득', '챙긴', '획득', '가져', '집어', '주워', '얻', '가방에']
        is_pickup_action = any(kw in user_input for kw in pickup_keywords)

        if is_discard_action:
            # 버리기 처리
            if item_name in inventory or str(item_name) in [str(i) for i in inventory]:
                world_state._remove_item(item_name)
                state['player_vars']['inventory'] = list(world_state.player['inventory'])

                sys_msg.append(f"🗑️ [{item_name}]을(를) 버렸습니다.")
                world_state.add_narrative_event(f"플레이어가 [{item_name}]을(를) 버림")
                logger.info(f"📦 [ITEM SYSTEM] Item discarded: {item_name}")
            else:
                sys_msg.append(f"⚠️ [{item_name}]이(가) 인벤토리에 없습니다.")
                logger.warning(f"⚠️ [ITEM_ACTION] Item not found in inventory for discard: {item_name}")

        elif is_pickup_action:
            # 줍기 처리
            world_state._add_item(item_name)
            state['player_vars']['inventory'] = list(world_state.player['inventory'])

            sys_msg.append(f"📦 [{item_name}]을(를) 획득했습니다!")
            world_state.add_narrative_event(f"플레이어가 [{item_name}]을(를) 습득함")
            logger.info(f"📦 [ITEM SYSTEM] Item acquired: {item_name}")
        else:
            # 사용 처리 (transition으로 처리되어야 하는 경우가 많음)
            logger.info(f"📦 [ITEM SYSTEM] Item use action detected, but should be handled by transition")
            sys_msg.append(f"⚠️ [{item_name}]을(를) 어떻게 사용하시겠습니까?")

        # system_message 저장
        state['system_message'] = " | ".join(sys_msg)

        # stuck_count 증가 (아이템 조작은 장면 이동이 아님)
        old_stuck_count = state.get('stuck_count', 0)
        state['stuck_count'] = old_stuck_count + 1
        world_state.stuck_count = state['stuck_count']
        logger.info(f"📈 [PROGRESS] stuck_count increased: {old_stuck_count} -> {state['stuck_count']} (item_action)")

        # world_state 저장
        state['world_state'] = world_state.to_dict()

        return state

    # ✅ 작업 2: investigate 의도 처리 - Scene Rule에서 스탯 변동 패싱 및 적용
    if state['parsed_intent'] == 'investigate':
        logger.info(f"🔍 [INVESTIGATE] Processing scene rule for investigation action")

        # 현재 장면의 rule 필드 가져오기
        scene_rule = curr_scene.get('rule', '') if curr_scene else ''

        if scene_rule:
            # 정규표현식으로 스탯 변동 패턴 추출: "Sanity -5", "HP +10", "Radiation +5" 등
            # 패턴: (스탯명) (부호)(숫자)
            stat_pattern = re.compile(r'(Sanity|HP|Gold|Radiation|sanity|hp|gold|radiation)\s*([+-]\d+)', re.IGNORECASE)
            matches = stat_pattern.findall(scene_rule)

            if matches:
                effects = []
                for stat_name, value_str in matches:
                    stat_name_lower = stat_name.lower()
                    value = int(value_str)  # +5 또는 -5 형태를 정수로 변환

                    effects.append({
                        "target": stat_name_lower,
                        "operation": "add",
                        "value": value
                    })

                    logger.info(f"📋 [RULE PARSED] {stat_name}: {value_str}")

                # WorldState에 효과 적용
                if effects:
                    world_state.update_state(effects)

                    # player_vars에도 동기화
                    for eff in effects:
                        key = eff["target"]
                        val = eff["value"]

                        current_val = state['player_vars'].get(key, 0)
                        if not isinstance(current_val, (int, float)):
                            current_val = 0

                        new_val = current_val + val
                        state['player_vars'][key] = new_val

                        # 시스템 메시지에 추가
                        if val > 0:
                            sys_msg.append(f"{key.upper()} +{val}")
                        else:
                            sys_msg.append(f"{key.upper()} {val}")

                    # 서사 이벤트 기록
                    stat_changes = ", ".join([f"{e['target']} {e['value']:+d}" for e in effects])
                    world_state.add_narrative_event(
                        f"조사 과정에서 상태 변화 발생: {stat_changes}"
                    )
                    logger.info(f"✅ [INVESTIGATE] Applied {len(effects)} stat changes from scene rule")
            else:
                logger.info(f"📋 [RULE PARSED] No stat changes found in scene rule")
        else:
            logger.info(f"📋 [RULE] No rule field found in current scene")

        # investigate는 장면 전환이 없으므로 stuck_count 증가
        if user_action:
            old_stuck_count = state.get('stuck_count', 0)
            state['stuck_count'] = old_stuck_count + 1
            logger.info(f"🔄 [INVESTIGATE] stuck_count: {old_stuck_count} -> {state['stuck_count']}")

    elif state['parsed_intent'] == 'transition' and 0 <= idx < len(transitions):
        trans = transitions[idx]
        effects = trans.get('effects', [])
        next_id = trans.get('target_scene_id')
        trigger_used = trans.get('trigger', 'unknown')

        logger.info(f"🎯 [TRANSITION] Attempting transition to: {next_id}")

        # ✅ 효과 적용을 WorldState로 일원화
        if effects:
            world_state.update_state(effects)
            # 효과가 player_vars에도 반영되도록 동기화
            for eff in effects:
                if isinstance(eff, dict):
                    key = eff.get("target", "").lower()
                    operation = eff.get("operation", "add")
                    raw_val = eff.get("value", 0)

                    # 아이템 효과
                    if operation in ["gain_item", "lose_item"]:
                        item_name = str(raw_val)
                        inventory = state['player_vars'].get('inventory', [])
                        if not isinstance(inventory, list):
                            inventory = []

                        if operation == "gain_item":
                            if item_name not in inventory:
                                inventory.append(item_name)
                            sys_msg.append(f"📦 획득: {item_name}")
                        elif operation == "lose_item":
                            if item_name in inventory:
                                inventory.remove(item_name)
                            sys_msg.append(f"🗑️ 사용: {item_name}")

                        state['player_vars']['inventory'] = inventory
                        continue

                    # 수치 효과
                    val = 0
                    if isinstance(raw_val, (int, float)):
                        val = int(raw_val)
                    elif isinstance(raw_val, str):
                        if raw_val.isdigit() or (raw_val.startswith('-') and raw_val[1:].isdigit()):
                            val = int(raw_val)

                    if key:
                        current_val = state['player_vars'].get(key, 0)
                        if not isinstance(current_val, (int, float)):
                            current_val = 0

                        if operation == "add":
                            new_val = current_val + val
                            if val > 0:
                                sys_msg.append(f"{key.upper()} +{val}")
                            else:
                                sys_msg.append(f"{key.upper()} {val}")
                        elif operation == "subtract":
                            new_val = max(0, current_val - abs(val))
                            sys_msg.append(f"{key.upper()} -{abs(val)}")
                        elif operation == "set":
                            new_val = val
                            sys_msg.append(f"{key.upper()} = {val}")
                        else:
                            new_val = current_val

                        state['player_vars'][key] = new_val

        # 씬 이동
        if next_id:
            # ✅ [작업 1-2] 장면 전환 성공 시 내러티브 기록의 from_scene은 반드시 actual_current_location 사용
            from_scene = actual_current_location
            logger.info(f"🔄 [TRANSITION] Using actual_current_location '{from_scene}' as from_scene for narrative")

            state['current_scene_id'] = next_id
            world_state.location = next_id

            # ✅ 작업 2: 장면 전환 성공 시 이전 씬의 출력 필드 명시적으로 제거
            state['npc_output'] = ''
            state['narrator_output'] = ''
            logger.info("🧹 [TRANSITION CLEANUP] Cleared output fields after scene transition")

            # ✅ [작업 4] 실제 이동이 일어난 경우에만 내러티브 기록 (from_scene != next_id)
            if from_scene != next_id:
                world_state.add_narrative_event(
                    f"유저가 '{trigger_used}'을(를) 통해 [{from_scene}]에서 [{next_id}]로 이동함"
                )
                logger.info(f"📖 [NARRATIVE] Recorded transition: [{from_scene}] -> [{next_id}] via '{trigger_used}'")
            else:
                logger.info(f"📖 [NARRATIVE] Skipped recording - same scene: [{from_scene}] == [{next_id}]")

            # ✅ 작업 2: 장면 전환 성공 시 stuck_count 초기화
            old_stuck_count = state.get('stuck_count', 0)
            state['stuck_count'] = 0
            logger.info(f"✅ [MOVE SUCCESS] {from_scene} -> {next_id} | stuck_count: {old_stuck_count} -> 0")
        else:
            # target_scene_id가 없는 경우 (비정상)
            state['stuck_count'] = state.get('stuck_count', 0) + 1
            logger.warning(f"⚠️ [TRANSITION FAILED] No target_scene_id | stuck_count: {state['stuck_count']}")

            # ✅ 작업 3: 장면 전환 실패 시 서사 기록
            if user_action:
                world_state.add_narrative_event(
                    f"유저가 '{user_action[:30]}...'을(를) 시도했으나 아무 일도 일어나지 않음"
                )
    else:
        # ✅ 작업 3: 장면 전환 실패 (씬 유지) 시 stuck_count 증가 및 서사 기록
        if user_action:
            old_stuck_count = state.get('stuck_count', 0)
            state['stuck_count'] = old_stuck_count + 1
            logger.info(
                f"🔄 [STUCK] Player stuck in scene '{actual_current_location}' | Intent: {state['parsed_intent']} | stuck_count: {old_stuck_count} -> {state['stuck_count']}")

            # 서사 이벤트 기록
            world_state.add_narrative_event(
                f"유저가 '{user_action[:30]}...'을(를) 시도했으나 장면 전환 없이 현재 위치에 머뭄"
            )
        else:
            logger.debug(f"⏸️ [NO INPUT] No user input, stuck_count unchanged: {state.get('stuck_count', 0)}")

    # 엔딩 체크
    if state['current_scene_id'] in all_endings:
        ending = all_endings[state['current_scene_id']]
        state['parsed_intent'] = 'ending'
        state['narrator_output'] = f"""
        <div class="my-8 p-8 border-2 border-yellow-500/50 bg-gradient-to-b from-yellow-900/40 to-black rounded-xl text-center fade-in shadow-2xl relative overflow-hidden">
            <h3 class="text-3xl font-black text-yellow-400 mb-4 tracking-[0.2em] uppercase drop-shadow-md">🎉 ENDING 🎉</h3>
            <div class="w-16 h-1 bg-yellow-500 mx-auto mb-6 rounded-full"></div>
            <div class="text-2xl font-bold text-white mb-4 drop-shadow-sm">"{ending.get('title')}"</div>
            <p class="text-gray-200 leading-relaxed text-lg serif-font">
                {ending.get('description')}
            </p>
        </div>
        """

    state['system_message'] = " | ".join(sys_msg)

    # 멱살 잡고 위치 강제 동기화
    world_state.location = state.get("current_scene_id", world_state.location)
    world_state.stuck_count = state.get("stuck_count", 0)

    logger.info(f"🎬 [DATA_SYNC] Synchronized world_state.location to {world_state.location}")

    # ✅ [작업 3] 최종 세이브 포인트 - 노드 끝나기 직전에 위치 일치 검증 및 강제
    final_scene_id = state.get('current_scene_id', '')
    final_ws_location = world_state.location

    if final_scene_id != final_ws_location:
        logger.error(
            f"❌ [FINAL SYNC ERROR] Mismatch detected before save! "
            f"state['current_scene_id']: '{final_scene_id}' vs world_state.location: '{final_ws_location}'"
        )
        # 강제로 world_state.location을 current_scene_id로 동기화 (state를 진실로 간주)
        world_state.location = final_scene_id
        logger.info(f"🔧 [FINAL SYNC FIX] Forced world_state.location = '{final_scene_id}'")

    # Assert: 최종 일치 확인
    assert state['current_scene_id'] == world_state.location, (
        f"[CRITICAL] Final location mismatch! "
        f"state: {state['current_scene_id']}, world_state: {world_state.location}"
    )
    logger.info(
        f"✅ [FINAL ASSERT] Location verified: state['current_scene_id'] == world_state.location == '{world_state.location}'")

    # ✅ WorldState 스냅샷 저장 (위치 동기화 후 저장)
    state['world_state'] = world_state.to_dict()
    logger.info(f"💾 [DB SNAPSHOT] Saved final state to DB with location: {world_state.location}")

    return state


def npc_node(state: PlayerState):
    """NPC 대화 (이동 아닐 때만 발동)"""

    # ✅ [FIX] 변수 미정의 해결: user_input과 curr_id를 최상단에 선언
    user_input = state.get('last_user_input', '').strip()
    curr_id = state.get('current_scene_id', '')

    # [추가] stuck_count 초기화 (state에 없으면 0으로 설정)
    if 'stuck_count' not in state:
        state['stuck_count'] = 0
        logger.info(f"🔧 [STUCK_COUNT] Initialized to 0 in npc_node")

    # WorldState 인스턴스 가져오기 및 복원
    scenario_id = state['scenario_id']
    world_state = WorldState()
    if 'world_state' in state and state['world_state']:
        world_state.from_dict(state['world_state'])

    # ========================================
    # 💀 작업 1: 죽은 NPC 대사 차단 → GM 나레이션으로 전환
    # ========================================
    target_npc = state.get('target_npc', '')

    # target_npc가 설정된 경우 NPC 상태 체크
    if target_npc:
        npc_state = world_state.get_npc_state(target_npc)
        if npc_state and npc_state.get('status') == 'dead':
            logger.info(f"💀 [NPC_NODE] Dead NPC '{target_npc}' detected. Redirecting output to Narrator.")

            # ✅ 작업 1: NPC 대사 차단, GM 나레이션으로 전환
            narrator_message = f"\n\n[GM]: 당신의 눈앞에는 차갑게 식어버린 {target_npc}의 시체만이 놓여 있습니다. 그는 더 이상 아무 말도 할 수 없습니다."
            state['narrator_output'] = narrator_message
            state['npc_output'] = ""  # NPC 이름표 제거

            # ✅ 작업 2: 내러티브 기록 보강
            world_state.add_narrative_event(f"유저가 죽은 {target_npc}에게 대화를 시도했지만 아무 응답도 없었음.")
            logger.info(f"📖 [NARRATIVE] Dead NPC interaction recorded: {target_npc}")

            # ✅ 작업 3: 데이터 동기화 유지
            world_state.location = state.get("current_scene_id", world_state.location)
            state['world_state'] = world_state.to_dict()
            logger.info(f"💾 [SYNC] World state saved after dead NPC interaction")

            return state

    # ✅ [작업 1] 턴 카운트 증가 로직을 함수 시작 부분으로 이동
    # 게임 시작이 아닐 때만 턴 증가 (Game Started는 Turn 1을 가져감)
    is_game_start = state.get('is_game_start', False)
    if not is_game_start:
        world_state.increment_turn()
        logger.info(f"⏱️ [TURN] Turn count increased to {world_state.turn_count} at npc_node start")
    else:
        logger.info(f"⏱️ [TURN] Game start - turn count not increased (current: {world_state.turn_count})")

    # [추가] 장면 전환 실패 (씬 유지) 시 stuck_count 증가
    curr_scene_id = state.get('current_scene_id', '')
    prev_scene_id = state.get('previous_scene_id', '')
    parsed_intent = state.get('parsed_intent', 'chat')

    # ========================================
    # ⚔️ 공격 의도 처리 (attack intent handling)
    # ========================================

    # 룰 기반 안전망: LLM이 chat으로 분류했어도 공격 동사가 있으면 attack으로 override
    attack_keywords = ['때리', '공격', '찌르', '베', '쏘', '죽이', '패', '가격', '해치', '치',
                       '무찌르', '처치', '타격', '구타', '폭행', '살해', '제거', '제압',
                       'attack', 'hit', 'strike', 'kill', 'murder', 'beat', 'punch', 'kick',
                       'stab', 'slash', 'shoot', 'harm', 'hurt', 'damage', 'destroy']

    has_attack_keyword = any(kw in user_input.lower() for kw in attack_keywords)

    if has_attack_keyword and parsed_intent == 'chat':
        logger.warning(f"⚔️ [SAFETY NET] Attack keyword detected in chat intent, overriding to 'attack'")
        parsed_intent = 'attack'
        state['parsed_intent'] = 'attack'

    # attack 의도 처리
    if parsed_intent == 'attack':
        logger.info(f"⚔️ [COMBAT] Attack intent detected. User input: '{user_input}'")

        # (a) 턴 증가는 이미 위에서 처리됨

        # (b) target_npc 추출
        target_npc = None

        # parsed_intent가 dict 형태로 저장되어 있을 수 있음 (JSON 파싱 결과)
        parsed_intent_data = state.get('_internal_flags', {}).get('parsed_intent_data', {})
        if isinstance(parsed_intent_data, dict):
            target_npc = parsed_intent_data.get('target_npc')

        # target_npc가 없으면 user_input에서 추출 시도
        if not target_npc:
            # 현재 씬의 NPC/적 목록
            all_scenes = {s['scene_id']: s for s in get_scenario_by_id(scenario_id)['scenes']}
            curr_scene = all_scenes.get(curr_id)

            if curr_scene:
                npc_list = curr_scene.get('npcs', []) + curr_scene.get('enemies', [])

                # user_input에서 NPC 이름 매칭 시도
                for npc_name in npc_list:
                    # 부분 매칭 (예: "노인" -> "노인 J")
                    if npc_name in user_input or npc_name.replace(' ', '').lower() in user_input.lower().replace(' ',
                                                                                                                 ''):
                        target_npc = npc_name
                        logger.info(f"🎯 [COMBAT] Target extracted from input: '{target_npc}'")
                        break

                # 그래도 못찾으면 world_state.find_npc_key 사용
                if not target_npc and npc_list:
                    # user_input에서 명사 추출 (간단한 휴리스틱)
                    for word in user_input.split():
                        potential_target = world_state.find_npc_key(word)
                        if potential_target:
                            target_npc = potential_target
                            logger.info(f"🎯 [COMBAT] Target found via find_npc_key: '{target_npc}'")
                            break

        # (c) target_npc가 확정되지 않으면 에러 처리
        if not target_npc:
            logger.warning(f"⚠️ [COMBAT] Attack target unclear. User input: '{user_input}'")
            state['system_message'] = "⚠️ 공격 대상이 불명확합니다. 누구를 공격하려는 건가요?"
            state['npc_output'] = ""

            # world_state 저장
            world_state.location = state.get("current_scene_id", world_state.location)
            state['world_state'] = world_state.to_dict()

            return state

        # (d) 데미지 산정 (random 2~6, 재현성을 위해 seed 옵션)
        import hashlib

        # 재현 가능한 난수 생성 (session_id + turn_count 기반)
        seed_string = f"{scenario_id}_{world_state.turn_count}_{target_npc}"
        seed_value = int(hashlib.md5(seed_string.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed_value)
        damage = rng.randint(2, 6)

        logger.info(f"🎲 [COMBAT] Damage roll: {damage} (seed: {seed_string})")

        # (e) world_state.damage_npc 호출
        combat_result = world_state.damage_npc(target_npc, damage)

        logger.info(f"⚔️ [COMBAT] Result: {combat_result}")

        # ========================================
        # 💥 작업 2: 플레이어 HP 동기화 - WorldState의 HP를 player_vars에 강제 동기화
        # ========================================
        world_state_hp = world_state.player.get("hp", 100)
        state['player_vars']['hp'] = world_state_hp
        logger.info(
            f"💾 [HP SYNC] Player HP synced to player_vars: {world_state_hp} (world_state.player['hp'] -> state['player_vars']['hp'])")

        # (f) system_message에 결과 저장
        state['system_message'] = combat_result

        # (g) narrative_history에 기록
        world_state.record_combat_event(f"플레이어가 {target_npc}을(를) 공격: {combat_result}")

        # ========================================
        # 💥 작업 2 & 4: 노드 종료 직전 최종 HP 동기화 강제 + 검증 로그
        # ========================================
        state['player_vars']['hp'] = world_state.player["hp"]
        logger.info(f"💾 [FINAL HP SYNC] Final Player HP sync before save: {world_state.player['hp']}")
        logger.info(f"💾 [DB PRE-SAVE] Final Player HP in state (npc_node): {state['player_vars']['hp']}")

        # (h) world_state 갱신
        state['world_state'] = world_state.to_dict()

        # NPC 대사는 생성하지 않음 (공격 결과만 표시)
        state['npc_output'] = ""

        # (i) 죽은 NPC 확인 및 대사 차단
        npc_state = world_state.get_npc_state(target_npc)
        if npc_state and npc_state.get('status') == 'dead':
            logger.info(f"💀 [COMBAT] {target_npc} is dead, blocking NPC dialogue")

            # ========================================
            # 💰 NPC 드랍 아이템 시스템
            # ========================================
            # 시나리오에서 NPC 데이터 조회
            scenario_data = get_scenario_by_id(scenario_id)
            npcs_data = scenario_data.get('npcs', [])

            # 해당 NPC의 drop_items 확인
            for npc_data in npcs_data:
                if npc_data.get('name') == target_npc:
                    drop_items = npc_data.get('drop_items', [])

                    if drop_items and isinstance(drop_items, list):
                        # 아이템 드랍 처리
                        for item_name in drop_items:
                            world_state._add_item(item_name)
                            logger.info(f"💰 [LOOT] {target_npc} dropped item: '{item_name}'")

                        # system_message에 전리품 정보 추가
                        items_text = ', '.join(drop_items)
                        loot_message = f"\n💰 전리품: {target_npc}에게서 [{items_text}]을(를) 획득했습니다!"
                        state['system_message'] += loot_message

                        # narrative_history에 기록
                        world_state.add_narrative_event(f"{target_npc} 처치 후 전리품 [{items_text}] 획득")

                        logger.info(f"💰 [LOOT] Total items dropped from target_npc: {len(drop_items)}")
                    else:
                        logger.info(f"💰 [LOOT] No items to drop from {target_npc}")
                    break

        logger.info(f"✅ [COMBAT] Attack processing complete. Damage: {damage}, Target: {target_npc}")

        return state

    # ========================================
    # 기존 로직 (chat/investigate/defend 등)
    # ========================================

    # ✅ 작업 1: stuck_count 증가 로직을 조기 리턴 전에 이동
    if user_input:
        old_stuck_count = state.get('stuck_count', 0)
        state['stuck_count'] = old_stuck_count + 1
        logger.info(
            f"🔄 [STUCK] Player stuck in scene '{curr_scene_id}' | Intent: {parsed_intent} | stuck_count: {old_stuck_count} -> {state['stuck_count']}")

        # ✅ 작업 4: investigate 의도일 때 서사 기록
        if parsed_intent == 'investigate':
            world_state.add_narrative_event(
                f"유저가 주변을 조사하며 '{user_input[:30]}...'을(를) 확인함"
            )
        # 다른 의도일 때도 기록 (defend 등)
        elif parsed_intent in ['defend']:
            world_state.add_narrative_event(
                f"유저가 '{user_input[:30]}...'을(를) 시도함"
            )

    # ✅ 작업 1: NPC 대사 생성은 'chat' 의도일 때만 실행
    if parsed_intent != 'chat':
        state['npc_output'] = ""
        # ✅ [작업 3] 백엔드 위치 데이터 강제 동기화 - DB 저장 전 최신 위치를 world_state에 덮어씌움
        world_state.location = state.get("current_scene_id", world_state.location)
        world_state.stuck_count = state.get("stuck_count", 0)
        state['world_state'] = world_state.to_dict()
        logger.info(
            f"🔄 [SYNC] Location synchronized in npc_node (early return): world_state.location = {world_state.location}, stuck_count = {world_state.stuck_count}")
        return

    # 기존 NPC 대화 로직
    curr_id = state['current_scene_id']
    all_scenes = {s['scene_id']: s for s in get_scenario_by_id(scenario_id)['scenes']}
    curr_scene = all_scenes.get(curr_id)
    npc_names = curr_scene.get('npcs', []) if curr_scene else []

    # [추가] 인벤토리 검증: 아이템 사용 시도 감지
    item_keywords = ['사용', '쓴다', '쏜다', '던진다', '먹는다', '마신다', '착용', '장착', '입는다',
                     'use', 'shoot', 'throw', 'eat', 'drink', 'wear', '뿌린다', '흔든다', '꺼낸다']

    if any(keyword in user_input.lower() for keyword in item_keywords):
        player_inventory = state.get('player_vars', {}).get('inventory', [])
        has_item = False

        for item in player_inventory:
            if item.lower() in user_input.lower():
                has_item = True
                break

        if not has_item:
            rejection_messages = [
                "주머니를 더듬어 보았지만 찾을 수 없습니다.",
                "소지품을 확인해보니 그것은 가지고 있지 않습니다.",
                "당신은 그 물건을 가지고 있지 않습니다.",
                "손을 뻗었지만 허공만 움켜쥐게 됩니다. 그것은 당신에게 없는 것입니다."
            ]
            state['npc_output'] = random.choice(rejection_messages)
            logger.info(f"🚫 [INVENTORY CHECK] Item not found in inventory. User input: {user_input}")
            return state

    # 기존 NPC 대화 로직
    if not npc_names:
        state['npc_output'] = ""
        return state

    target_npc_name = npc_names[0]
    npc_info = {"name": target_npc_name, "role": "Unknown", "personality": "보통"}

    for npc in get_scenario_by_id(scenario_id).get('npcs', []):
        if npc.get('name') == target_npc_name:
            npc_info['role'] = npc.get('role', 'Unknown')
            npc_info['personality'] = npc.get('personality', '보통')
            npc_info['dialogue_style'] = npc.get('dialogue_style', '')
            break

    history = state.get('history', [])
    history_context = "\n".join(history[-3:]) if history else "대화 시작"

    # [추가] 현재 장면의 transitions_hints와 stuck_level 추출
    transitions_list = []
    if curr_scene:
        for t in curr_scene.get('transitions', []):
            trigger = t.get('trigger', '알 수 없음')
            transitions_list.append(trigger)

    transitions_hints = ", ".join(transitions_list) if transitions_list else "힌트 없음"
    stuck_level = state.get('stuck_count', 0)

    # YAML에서 프롬프트 로드
    prompts = load_player_prompts()
    prompt_template = prompts.get('npc_dialogue', '')

    # ✅ WorldState 컨텍스트 추가
    world_context = world_state.get_llm_context()

    if prompt_template:
        scenario = get_scenario_by_id(scenario_id)
        player_status = format_player_status(scenario, state.get('player_vars', {}))

        # [수정] WorldState 컨텍스트를 프롬프트에 포함
        prompt = f"""{world_context}

{prompt_template.format(
            player_status=player_status,
            npc_name=npc_info['name'],
            npc_role=npc_info['role'],
            npc_personality=npc_info['personality'],
            history_context=history_context,
            user_input=user_input,
            transitions_hints=transitions_hints,
            stuck_level=stuck_level
        )}"""
    else:
        # 폴백 프롬프트 (YAML 로드 실패 시)
        logger.warning("⚠️ Failed to load npc_dialogue from YAML, using fallback")
        prompt = f"""{world_context}

당신은 텍스트 RPG의 NPC입니다.
이름: {npc_info['name']}, 역할: {npc_info['role']}, 성격: {npc_info['personality']}
플레이어: "{user_input}"
NPC로서 1-2문장으로 응답하세요."""

    try:
        api_key = os.getenv("OPENROUTER_API_KEY")
        model_name = state.get('model', 'openai/tngtech/deepseek-r1t2-chimera:free')
        llm = get_cached_llm(api_key=api_key, model_name=model_name, streaming=False)
        response = llm.invoke(prompt).content.strip()

        # [추가] 응답 검증 - 사용자 입력을 그대로 반복하는 경우 LLM으로 재생성
        normalized_input = user_input.lower().replace(" ", "")
        normalized_response = response.lower().replace(" ", "")

        if normalized_input in normalized_response and len(normalized_response) < len(normalized_input) + 10:
            # 사용자 입력을 단순 반복한 경우 폴백 프롬프트로 재시도
            logger.warning(f"⚠️ NPC response too similar to user input, retrying with fallback prompt")
            fallback_template = prompts.get('npc_fallback', '')
            if fallback_template:
                fallback_prompt = fallback_template.format(
                    npc_name=npc_info['name'],
                    npc_role=npc_info['role'],
                    user_input=user_input
                )
                response = llm.invoke(fallback_prompt).content.strip()

        state['npc_output'] = response

        # ✅ 작업 2: NPC 대화 서사 요약 및 기록 - LLM을 활용하여 대화 핵심 내용 요약
        try:
            # 대화 요약 프롬프트 생성
            summary_prompt = f"""다음 대화를 한 문장으로 간결하게 요약하세요:
플레이어: "{user_input}"
NPC ({target_npc_name}): "{response}"

요약 형식: "플레이어가 [NPC]에게 [행동/요청]했고, NPC는 [반응]함"
예시: "플레이어가 노인 J에게 술집을 불태우겠다고 협박하며 지도를 요구했고, 노인은 겁에 질려 반응함"

요약:"""

            summary_llm = get_cached_llm(api_key=api_key, model_name=model_name, streaming=False)
            conversation_summary = summary_llm.invoke(summary_prompt).content.strip()

            # 요약이 너무 길면 잘라내기
            if len(conversation_summary) > 100:
                conversation_summary = conversation_summary[:97] + "..."

            world_state.add_narrative_event(conversation_summary)
            logger.info(f"📖 [NPC DIALOGUE] Summary added to narrative: {conversation_summary}")

        except Exception as summary_error:
            # 요약 실패 시 간단한 템플릿 사용
            logger.warning(f"⚠️ Failed to generate conversation summary: {summary_error}")
            fallback_summary = f"플레이어가 '{target_npc_name}'와 대화함 (주제: {user_input[:20]}...)"
            world_state.add_narrative_event(fallback_summary)

        if 'history' not in state: state['history'] = []
        state['history'].append(f"User: {user_input}")
        state['history'].append(f"NPC({target_npc_name}): {response}")

        logger.info(f"💬 [NPC] {target_npc_name}: {response}")

    except Exception as e:
        logger.error(f"NPC generation error: {e}")
        # 에러 시에도 LLM으로 간단한 응답 생성 시도
        try:
            fallback_template = prompts.get('npc_fallback', '')
            if fallback_template:
                fallback_prompt = fallback_template.format(
                    npc_name=npc_info['name'],
                    npc_role=npc_info['role'],
                    user_input=user_input
                )
                api_key = os.getenv("OPENROUTER_API_KEY")
                llm = get_cached_llm(api_key=api_key, model_name='openai/gpt-3.5-turbo', streaming=False)
                state['npc_output'] = llm.invoke(fallback_prompt).content.strip()
            else:
                state['npc_output'] = ""
        except Exception:
            state['npc_output'] = ""

    # ✅ 작업 3: 백엔드 위치 데이터 강제 동기화 - DB 저장 전 최신 위치를 world_state에 덮어씌움
    world_state.location = state.get("current_scene_id", world_state.location)
    world_state.stuck_count = state.get("stuck_count", 0)

    logger.info(f"🎬 [DATA_SYNC] Synchronized world_state.location to {world_state.location}")

    # ✅ [작업 3] 최종 세이브 포인트 - 노드 끝나기 직전에 위치 일치 검증 및 강제
    final_scene_id = state.get('current_scene_id', '')
    final_ws_location = world_state.location

    if final_scene_id != final_ws_location:
        logger.error(
            f"❌ [NPC_NODE FINAL SYNC ERROR] Mismatch detected before save! "
            f"state['current_scene_id']: '{final_scene_id}' vs world_state.location: '{final_ws_location}'"
        )
        # 강제로 world_state.location을 current_scene_id로 동기화 (state를 진실로 간주)
        world_state.location = final_scene_id
        logger.info(f"🔧 [NPC_NODE FINAL SYNC FIX] Forced world_state.location = '{final_scene_id}'")

    # Assert: 최종 일치 확인
    assert state['current_scene_id'] == world_state.location, (
        f"[CRITICAL] NPC_NODE final location mismatch! "
        f"state: {state['current_scene_id']}, world_state: {world_state.location}"
    )
    logger.info(
        f"✅ [NPC_NODE FINAL ASSERT] Location verified: state['current_scene_id'] == world_state.location == '{world_state.location}'")

    # WorldState 스냅샷 저장 (위치 동기화 후 저장)
    state['world_state'] = world_state.to_dict()
    logger.info(
        f"🔄 [SYNC] Location synchronized in npc_node: world_state.location = {world_state.location}, stuck_count = {world_state.stuck_count}")
    logger.info(f"💾 [DB SNAPSHOT] Saved final state to DB with location: {world_state.location}")

    return state


def check_npc_appearance(state: PlayerState) -> str:
    """NPC 및 적 등장 (LLM 기반 생성)"""
    scenario_id = state['scenario_id']
    curr_id = state['current_scene_id']

    # 씬 변경 없으면 등장 메시지 생략
    if state.get('previous_scene_id') == curr_id:
        return ""

    scenario = get_scenario_by_id(scenario_id)
    all_scenes = {s['scene_id']: s for s in scenario['scenes']}
    curr_scene = all_scenes.get(curr_id)
    if not curr_scene: return ""

    # [FIX] NPC와 적을 모두 처리 - 딕셔너리에서 이름만 추출
    raw_npcs = curr_scene.get('npcs', [])
    npc_names = [n.get('name') if isinstance(n, dict) else n for n in raw_npcs]
    enemy_names = curr_scene.get('enemies', [])
    scene_type = curr_scene.get('type', 'normal')
    scene_title = curr_scene.get('title', 'Untitled')

    if not npc_names and not enemy_names: return ""

    scene_history_key = f"npc_appeared_{curr_id}"
    player_vars = state.get('player_vars', {})
    if player_vars.get(scene_history_key): return ""

    state['player_vars'][scene_history_key] = True
    introductions = []

    # YAML에서 프롬프트 로드
    prompts = load_player_prompts()
    api_key = os.getenv("OPENROUTER_API_KEY")
    model_name = state.get('model', 'openai/tngtech/deepseek-r1t2-chimera:free')

    # [FIX] 장면 유형에 따른 메시지 - LLM으로 생성
    if scene_type == 'battle':
        battle_start_template = prompts.get('battle_start', '')
        if battle_start_template:
            battle_start_prompt = battle_start_template.format(
                scene_title=scene_title,
                enemy_names=', '.join(enemy_names) if enemy_names else '알 수 없는 적'
            )
            try:
                llm = get_cached_llm(api_key=api_key, model_name=model_name, streaming=False)
                battle_msg = llm.invoke(battle_start_prompt).content.strip()
                introductions.append(f"""
                <div class='battle-alert text-red-400 font-bold my-3 p-3 bg-red-900/30 rounded border-2 border-red-500 animate-pulse'>
                    ⚔️ {battle_msg}
                </div>
                """)
            except Exception as e:
                logger.error(f"Battle start message generation error: {e}")
                introductions.append("""
                <div class='battle-alert text-red-400 font-bold my-3 p-3 bg-red-900/30 rounded border-2 border-red-500 animate-pulse'>
                    ⚔️ 전투가 시작됩니다!
                </div>
                """)

    # NPC 등장 - LLM으로 생성
    if npc_names:
        npc_appearance_template = prompts.get('npc_appearance', '')
        for npc_data in npc_names:
            # 🔴 [CRITICAL] NPC 데이터 정규화 및 이미지 추출
            if isinstance(npc_data, dict):
                real_npc_name = npc_data.get('name', 'Unknown NPC')
                if 'image' in npc_data and npc_data['image']:
                    # [FIX] 내부 URL 치환을 위해 get_minio_url 호출
                    minio_npc_url = get_minio_url('npcs', npc_data['image'])
                else:
                    minio_npc_url = get_minio_url('npcs', real_npc_name)
            else:
                real_npc_name = str(npc_data)
                minio_npc_url = get_minio_url('npcs', real_npc_name)

            # NPC 역할 찾기
            npc_role = "Unknown"
            for npc in get_scenario_by_id(scenario_id).get('npcs', []):
                if npc.get('name') == real_npc_name:
                    npc_role = npc.get('role', 'Unknown')
                    break

            if npc_appearance_template:
                npc_prompt = npc_appearance_template.format(
                    scene_title=scene_title,
                    npc_name=real_npc_name,
                    npc_role=npc_role
                )
                try:
                    llm = get_cached_llm(api_key=api_key, model_name=model_name, streaming=False)
                    npc_action = llm.invoke(npc_prompt).content.strip()
                    intro_html = f"""
                    <div class='npc-intro flex items-center gap-3 my-2 p-3 bg-green-900/20 rounded border-l-2 border-green-500'>
                        <div class="relative w-12 h-12 flex-shrink-0">
                            <img src="{minio_npc_url}" class="w-12 h-12 rounded-full border-2 border-green-500 shadow-green-500/50 object-cover block" alt="{real_npc_name}">
                        </div>
                        <span class="text-green-300 italic">👀 {npc_action}</span>
                    </div>
                    """
                    introductions.append(intro_html)
                except Exception as e:
                    logger.error(f"NPC appearance generation error: {e}")
                    intro_html = f"""
                    <div class='npc-intro flex items-center gap-3 my-2 p-3 bg-green-900/20 rounded border-l-2 border-green-500'>
                        <div class="relative w-12 h-12 flex-shrink-0">
                            <img src="{minio_npc_url}" class="w-12 h-12 rounded-full border-2 border-green-500 shadow-green-500/50 object-cover block" alt="{real_npc_name}">
                        </div>
                        <div class="text-green-300 italic">
                            👀 <span class='font-bold'>{real_npc_name}</span>이(가) 당신을 바라봅니다.
                        </div>
                    </div>
                    """
                    introductions.append(intro_html)
            else:
                intro_html = f"""
                <div class='npc-intro flex items-center gap-3 my-2 p-3 bg-green-900/20 rounded border-l-2 border-green-500'>
                    <div class="relative w-12 h-12 flex-shrink-0">
                        <img src="{minio_npc_url}" class="w-12 h-12 rounded-full border-2 border-green-500 shadow-green-500/50 object-cover block" alt="{real_npc_name}">
                    </div>
                    <div class="text-green-300 italic">
                        👀 <span class='font-bold'>{real_npc_name}</span>이(가) 당신을 바라봅니다.
                    </div>
                </div>
                """
                introductions.append(intro_html)

    # [FIX] 적 등장 처리 - LLM으로 생성
    if enemy_names:
        enemy_appearance_template = prompts.get('enemy_appearance', '')
        for enemy_data in enemy_names:
            # 🔴 [CRITICAL] enemy_data가 딕셔너리인지 문자열인지 확인하여 정규화
            if isinstance(enemy_data, dict):
                real_enemy_name = enemy_data.get('name', 'Unknown Enemy')
                # 딕셔너리에 image 필드가 있으면 우선 사용, 없으면 MinIO 생성
                if 'image' in enemy_data and enemy_data['image']:
                    # [FIX] 내부 URL 치환을 위해 get_minio_url 호출
                    minio_enemy_url = get_minio_url('enemies', enemy_data['image'])
                else:
                    minio_enemy_url = get_minio_url('enemies', real_enemy_name)
            else:
                real_enemy_name = str(enemy_data)
                minio_enemy_url = get_minio_url('enemies', real_enemy_name)

            if enemy_appearance_template:
                enemy_prompt = enemy_appearance_template.format(
                    scene_title=scene_title,
                    enemy_name=real_enemy_name
                )
                try:
                    llm = get_cached_llm(api_key=api_key, model_name=model_name, streaming=False)
                    enemy_action = llm.invoke(enemy_prompt).content.strip()
                    intro_html = f"""
                    <div class='enemy-intro flex items-center gap-3 my-2 p-3 bg-red-900/30 rounded border-l-2 border-red-500'>
                        <div class="relative w-12 h-12 flex-shrink-0">
                            <img src="{minio_enemy_url}" class="w-12 h-12 rounded-full border-2 border-red-500 shadow-red-500/50 object-cover block" alt="{real_enemy_name}">
                        </div>
                        <span class="text-red-400 font-bold">⚔️ {enemy_action}</span>
                    </div>
                    """
                    introductions.append(intro_html)
                except Exception as e:
                    logger.error(f"Enemy appearance generation error: {e}")
                    intro_html = f"""
                    <div class='enemy-intro flex items-center gap-3 my-2 p-3 bg-red-900/30 rounded border-l-2 border-red-500'>
                        <div class="relative w-12 h-12 flex-shrink-0">
                            <img src="{minio_enemy_url}" class="w-12 h-12 rounded-full border-2 border-red-500 shadow-red-500/50 object-cover block" alt="{real_enemy_name}">
                        </div>
                        <div class="text-red-400 font-bold">
                            ⚔️ <span class='font-bold'>{real_enemy_name}</span>이(가) 나타났습니다!
                        </div>
                    </div>
                    """
                    introductions.append(intro_html)
            else:
                intro_html = f"""
                <div class='enemy-intro flex items-center gap-3 my-2 p-3 bg-red-900/30 rounded border-l-2 border-red-500'>
                    <div class="relative w-12 h-12 flex-shrink-0">
                        <img src="{minio_enemy_url}" class="w-12 h-12 rounded-full border-2 border-red-500 shadow-red-500/50 object-cover block" alt="{real_enemy_name}">
                    </div>
                    <div class="text-red-400 font-bold">
                        ⚔️ <span class='font-bold'>{real_enemy_name}</span>이(가) 나타났습니다!
                    </div>
                </div>
                """
                introductions.append(intro_html)

    return "\n".join(introductions)


def narrator_node(state: PlayerState):
    """내레이터 - GM 나레이션 (최종 출력 생성)"""

    # ✅ [FIX] 변수 미정의 해결: user_input을 최상단에 선언
    user_input = state.get('last_user_input', '').strip()

    # [추가] stuck_count 초기화 (state에 없으면 0으로 설정)
    if 'stuck_count' not in state:
        state['stuck_count'] = 0
        logger.info(f"🔧 [STUCK_COUNT] Initialized to 0 in narrator_node")

    # WorldState 인스턴스 가져오기 및 복원
    scenario_id = state['scenario_id']
    world_state = WorldState()

    # 기존 world_state가 있으면 복원
    if 'world_state' in state and state['world_state']:
        world_state.from_dict(state['world_state'])
    else:
        # 처음 생성하는 경우 시나리오로 초기화
        scenario = get_scenario_by_id(scenario_id)
        world_state.initialize_from_scenario(scenario)

    # ✅ [작업 1] 턴 카운트 증가 로직을 함수 시작 부분으로 이동
    # 게임 시작이 아닐 때만 턴 증가 (Game Started는 Turn 1을 가져감)
    is_game_start = state.get('is_game_start', False)
    if not is_game_start:
        world_state.increment_turn()
        logger.info(f"⏱️ [TURN] Turn count increased to {world_state.turn_count} at narrator_node start")
    else:
        logger.info(f"⏱️ [TURN] Game start - turn count not increased (current: {world_state.turn_count})")

    # WorldState 스냅샷 저장
    state['world_state'] = world_state.to_dict()

    return state


# --- Streaming Generators (SSE) ---

def prologue_stream_generator(state: PlayerState):
    # [FIX] scenario_id로 시나리오 조회
    scenario_id = state.get('scenario_id')
    if not scenario_id:
        yield "이야기가 시작됩니다..."
        return

    scenario = get_scenario_by_id(scenario_id)
    if not scenario:
        yield "이야기가 시작됩니다..."
        return

    prologue_text = scenario.get('prologue', scenario.get('prologue_text', ''))
    if not prologue_text:
        yield "이야기가 시작됩니다..."
        return
    yield prologue_text


def get_narrative_fallback_message(scenario: Dict[str, Any]) -> str:
    """세계관별 폴백 메시지 - YAML에서 로드"""
    genre = scenario.get('genre', '').lower()
    world_setting = scenario.get('world_setting', '').lower()

    # YAML에서 폴백 메시지 로드
    prompts = load_player_prompts()
    fallback_messages = prompts.get('fallback_messages', {})

    if not fallback_messages:
        # 기본 폴백 메시지
        fallback_messages = {
            'cyberpunk': "⚠️ 신경 신호가 불안정하여 시야가 일시적으로 차단되었습니다. 잠시 후 다시 시도하십시오.",
            'sf': "⚠️ 통신 간섭이 감지되었습니다. 신호가 안정화될 때까지 대기해 주세요.",
            'fantasy': "⚠️ 마력의 흐름이 일시적으로 혼란스럽습니다. 잠시 정신을 가다듬어 주세요.",
            'horror': "⚠️ 알 수 없는 힘이 시야를 가립니다... 잠시 후 다시 시도해 주세요.",
            'modern': "⚠️ 잠시 정신이 혼미해졌습니다. 심호흡을 하고 다시 시도해 주세요.",
            'medieval': "⚠️ 갑작스러운 현기증이 엄습합니다. 잠시 쉬었다가 다시 시도해 주세요.",
            'apocalypse': "⚠️ 방사능 간섭으로 인해 감각이 일시적으로 마비되었습니다. 잠시 후 다시 시도하십시오.",
            'workplace': "⚠️ 과로로 인해 잠시 멍해졌습니다. 커피를 마시고 다시 시도해 주세요.",
            'martial': "⚠️ 내공의 흐름이 일시적으로 막혔습니다. 기를 가다듬고 다시 시도하십시오.",
            'default': "⚠️ 잠시 상황 파악이 어렵습니다. 심호흡을 하고 다시 시도해 주세요."
        }

    for key, message in fallback_messages.items():
        if key != 'default' and (key in genre or key in world_setting):
            return message

    return fallback_messages.get('default', "")


def _stream_and_track(llm, prompt, user_id, model_name):
    """
    LLM 스트리밍 및 토큰 과금 헬퍼
    """
    prompt_tokens = 0
    completion_tokens = 0
    total_cost = 0

    # stream
    content_chunks = []
    for chunk in llm.stream(prompt):
        if chunk.content:
            content_chunk = chunk.content
            content_chunks.append(content_chunk)
            yield content_chunk

        # LangChain usage metadata capture
        if hasattr(chunk, 'usage_metadata') and chunk.usage_metadata:
            # Usually the last chunk has the total
            prompt_tokens = chunk.usage_metadata.get('input_tokens', 0)
            completion_tokens = chunk.usage_metadata.get('output_tokens', 0)

    # Billing
    if user_id and (prompt_tokens > 0 or completion_tokens > 0):
        try:
            cost = UserService.calculate_llm_cost(model_name, prompt_tokens, completion_tokens)
            total_cost = cost
            UserService.deduct_tokens(user_id, cost, "narrative_stream", model_name, prompt_tokens + completion_tokens)
            
            # [NEW] 토큰 소모 정보 로깅
            logger.info(f"[GAME TOKEN] User: {user_id}, Model: {model_name}, Cost: {cost} CR, Tokens: {prompt_tokens + completion_tokens}")
        except Exception as e:
            logger.error(f"Billing error in stream: {e}")
    
    # [NEW] 토큰 소모 정보 반환 (프론트에서 표시용)
    token_info = {
        "tokens_used": prompt_tokens + completion_tokens,
        "cost": total_cost,
        "model": model_name
    }
    
    # 토큰 정보를 별도로 yield
    yield token_info


def scene_stream_generator(state: PlayerState, retry_count: int = 0, max_retries: int = 2, user_id: str = None):
    """
    [2단계 API 호출 구조 - 2단계: 서사 생성]
    1단계에서 분류된 의도(parsed_intent)에 따라 전용 서사 프롬프트를 선택하여 스트리밍

    나레이션 모드:
    [MODE 1] 씬 유지 + 의도별 분기 (investigate/attack/defend/chat/near_miss)
    [MODE 2] 씬 변경 -> 장면 묘사
    """
    # [NEW] user_id 추출 (함수 인자 또는 state에서)
    if not user_id:
        user_id = state.get('user_id')

    scenario_id = state['scenario_id']
    curr_id = state['current_scene_id']
    prev_id = state.get('previous_scene_id')
    user_input = state.get('last_user_input', '')
    parsed_intent = state.get('parsed_intent', 'chat')

    scenario = get_scenario_by_id(scenario_id)
    all_scenes = {s['scene_id']: s for s in scenario['scenes']}
    all_endings = {e['ending_id']: e for e in scenario.get('endings', [])}

    # WorldState 인스턴스 가져오기
    world_state = WorldState()
    if 'world_state' in state and state['world_state']:
        world_state.from_dict(state['world_state'])

    # ========================================
    # 현재 씬 정보 추출 (scene_title, scene_type, npc_names, enemy_names)
    # ========================================
    curr_scene = all_scenes.get(curr_id) if curr_id not in all_endings else None
    scene_title = ""
    scene_type = "normal"
    npc_names = []
    enemy_names = []

    if curr_scene:
        scene_title = curr_scene.get('title', curr_id)
        scene_type = curr_scene.get('type', 'normal')

        # 🔴 [CRITICAL] NPC 이름 정규화: 딕셔너리면 name 필드 추출
        raw_npcs = curr_scene.get('npcs', [])
        npc_names = [n.get('name') if isinstance(n, dict) else n for n in raw_npcs]

        # 🔴 [CRITICAL] 적 이름 정규화: 딕셔너리면 name 필드 추출
        raw_enemies = curr_scene.get('enemies', [])
        enemy_names = [e.get('name') if isinstance(e, dict) else e for e in raw_enemies]

        logger.info(f"🎬 [SCENE INFO] NPCs: {npc_names}, Enemies: {enemy_names}")

    # ========================================
    # 💀 작업 2: 죽은 NPC 상태 정보 수집 (환각 방지)
    # ========================================
    npc_status_context = ""

    if curr_scene:
        all_npc_names = npc_names + enemy_names

        dead_npcs = []
        for npc_name in all_npc_names:
            # 🔴 [SAFETY] NPC 이름이 None이거나 빈 문자열이면 스킵
            if not npc_name:
                continue

            npc_state = world_state.get_npc_state(npc_name)
            if npc_state and npc_state.get('status') == 'dead':
                dead_npcs.append(npc_name)

        if dead_npcs:
            dead_list = ", ".join(dead_npcs)
            npc_status_context = f"""
⚠️ **[CRITICAL INSTRUCTION - NPC STATUS]**
다음 NPC들은 현재 'dead' 상태입니다: {dead_list}

**절대적 규칙:**
- 이들은 이미 사망했으므로, 움직이거나 말하거나 행동하는 묘사를 절대 하지 마세요.
- 시체, 주검, 차갑게 식은 몸 등으로만 표현하세요.
- 이들이 살아있는 것처럼 묘사하면 중대한 오류입니다.
"""
            logger.info(f"💀 [NARRATOR] Dead NPCs in scene: {dead_list}")

    # system_message에 시체 관련 내용이 있으면 최우선 반영
    system_message = state.get('system_message', '')
    if "시체" in system_message or "식어버린" in system_message:
        npc_status_context += f"\n⚠️ **시스템 메시지 최우선 반영:** {system_message}\n"
        logger.info(f"💀 [NARRATOR] Corpse-related system message detected: {system_message}")

    # =============================================================================
    # [MODE 1] 씬 유지됨 -> 의도(parsed_intent)에 따른 전용 서사 프롬프트 선택
    # =============================================================================
    if prev_id == curr_id and user_input:
        prompts = load_player_prompts()
        weakness_hint = get_npc_weakness_hint(scenario, enemy_names) or "주변을 살펴보니 활용할 수 있는 것이 보입니다."

        # [2단계] parsed_intent에 따라 전용 프롬프트 선택
        prompt_template = None
        prompt_key = None
        narrative_prompt = ""  # 초기화

        if parsed_intent == 'investigate':
            # 조사/탐색 행동
            prompt_key = 'battle_investigation' if scene_type == 'battle' else 'battle_investigation'
            prompt_template = prompts.get(prompt_key, '')
            if prompt_template:
                narrative_prompt = prompt_template.format(
                    user_input=user_input,
                    scene_title=scene_title,
                    weakness_hint=weakness_hint if weakness_hint else "주변을 살펴보니 특이한 물건이 눈에 띕니다."
                )

        elif parsed_intent == 'attack':
            # 공격 행동 (승리 조건 미충족)
            prompt_key = 'battle_attack_result'
            prompt_template = prompts.get(prompt_key, '')
            if prompt_template:
                narrative_prompt = prompt_template.format(
                    user_input=user_input,
                    scene_title=scene_title,
                    weakness_hint=weakness_hint
                )

        elif parsed_intent == 'defend':
            # 방어 행동
            prompt_key = 'battle_defense'
            prompt_template = prompts.get(prompt_key, '')
            if prompt_template:
                narrative_prompt = prompt_template.format(
                    user_input=user_input,
                    scene_title=scene_title,
                    weakness_hint=weakness_hint
                )

        # Near Miss 처리
        near_miss = state.get('near_miss_trigger')
        if near_miss and parsed_intent == 'chat':
            prompt_key = 'near_miss'
            prompt_template = prompts.get(prompt_key, '')
            if prompt_template:
                player_status = format_player_status(scenario, state.get('player_vars', {}))

                narrative_prompt = prompt_template.format(
                    user_input=user_input,
                    player_status=player_status,
                    near_miss_trigger=near_miss
                )
                logger.info(f"🎬 [NARRATIVE] Using prompt: near_miss for near miss situation")

        # 의도별 프롬프트가 설정되었으면 LLM 스트리밍
        if prompt_template and 'narrative_prompt' in locals() and narrative_prompt:
            try:
                api_key = os.getenv("OPENROUTER_API_KEY")
                model_name = state.get('model', 'openai/tngtech/deepseek-r1t2-chimera:free')
                llm = get_cached_llm(api_key=api_key, model_name=model_name, streaming=True)

                logger.info(f"🎬 [NARRATIVE] Using prompt: {prompt_key} for intent: {parsed_intent}")

                # [NEW] _stream_and_track 사용 및 토큰 정보 수집
                token_info = None
                content_chunks = []
                
                for chunk in _stream_and_track(llm, narrative_prompt, user_id, model_name):
                    if isinstance(chunk, dict):
                        # 토큰 정보 수신
                        token_info = chunk
                    else:
                        # 일반 콘텐츠
                        content_chunks.append(chunk)
                        yield chunk
                
                # 🔥 [NEW] 토큰 소모 정보 전송 (프론트 표시용)
                if token_info and token_info.get('cost', 0) > 0:
                    token_data = {
                        "type": "token_usage",
                        "tokens_used": token_info.get('tokens_used', 0),
                        "cost": token_info.get('cost', 0),
                        "model": token_info.get('model', 'unknown'),
                        "action": "narrative"
                    }
                    yield f"__TOKEN_INFO__{json.dumps(token_data)}__"
                
                return

            except Exception as e:
                logger.error(f"Narrative generation error for intent '{parsed_intent}': {e}")
                # 폴백 메시지
                if parsed_intent == 'investigate':
                    if weakness_hint:
                        yield f"주변을 살핍니다. {weakness_hint}"
                    else:
                        yield "주변을 둘러보니 활용할 수 있는 것들이 보입니다."
                    return
                elif parsed_intent == 'attack':
                    attack_msg = random.choice(get_battle_attack_messages())
                    yield attack_msg
                    
                    # 전투 후 추가 행동 유도
                    battle_hints = [
                        "적이 쓰러졌습니다. 이제 어떻게 하시겠습니까?",
                        "전투가 끝났습니다. 다음 행동을 선택해주세요.",
                        "승리했습니다! 계속해서 앞으로 나아가시겠습니까?",
                        "적을 물리쳤습니다. 주변을 조사하거나 다른 곳으로 이동할 수 있습니다."
                    ]
                    yield random.choice(battle_hints)
                    return
                elif parsed_intent == 'defend':
                    yield random.choice(get_battle_defensive_messages())
                    return
                elif near_miss:
                    yield random.choice(get_near_miss_narrative_hints())
                    return

        # NPC 대화가 있으면 나레이션 스킵
        npc_output = state.get('npc_output', '')
        if npc_output:
            yield ""
            return

        # 전투 씬에서 일반 chat 행동 (프롬프트 없을 때)
        if scene_type == 'battle' and parsed_intent == 'chat':
            battle_continue_template = prompts.get('battle_continue', '')
            if battle_continue_template:
                battle_continue_prompt = battle_continue_template.format(
                    user_input=user_input,
                    scene_title=scene_title,
                    weakness_hint=weakness_hint
                )
                try:
                    api_key = os.getenv("OPENROUTER_API_KEY")
                    model_name = state.get('model', 'openai/tngtech/deepseek-r1t2-chimera:free')
                    llm = get_cached_llm(api_key=api_key, model_name=model_name, streaming=True)

                    token_info = None
                    content_chunks = []
                    
                    for chunk in _stream_and_track(llm, battle_continue_prompt, user_id, model_name):
                        if isinstance(chunk, dict):
                            # 
                            token_info = chunk
                        else:
                            # 
                            content_chunks.append(chunk)
                            yield chunk
                    
                    #  [NEW] 
                    if token_info and token_info.get('cost', 0) > 0:
                        token_data = {
                            "type": "token_usage",
                            "tokens_used": token_info.get('tokens_used', 0),
                            "cost": token_info.get('cost', 0),
                            "model": token_info.get('model', 'unknown'),
                            "action": "battle_continue"
                        }
                        yield f"__TOKEN_INFO__{json.dumps(token_data)}__"
                    
                    return

                except Exception:
                    yield random.choice(get_battle_stalemate_messages())
                return

        # 
        if parsed_intent == 'chat' and not npc_output:
            transitions = curr_scene.get('transitions', [])
            filtered_transitions = filter_negative_transitions(transitions, scenario)

            if filtered_transitions:
                # transitions_hints 생성
                transitions_hints = "\n".join([f"- {t.get('trigger', '')}" for t in filtered_transitions])

                hint_mode_template = prompts.get('hint_mode', '')
                if hint_mode_template:
                    player_status = format_player_status(scenario, state.get('player_vars', {}))

                    # [추가] stuck_count를 stuck_level로 전달
                    stuck_level = state.get('stuck_count', 0)

                    hint_prompt = hint_mode_template.format(
                        user_input=user_input,
                        player_status=player_status,
                        scene_title=scene_title,
                        transitions_hints=transitions_hints,
                        stuck_level=stuck_level
                    )
                    try:
                        api_key = os.getenv("OPENROUTER_API_KEY")
                        model_name = state.get('model', 'openai/tngtech/deepseek-r1t2-chimera:free')
                        llm = get_cached_llm(api_key=api_key, model_name=model_name, streaming=True)

                        logger.info(f"💡 [HINT MODE] stuck_level: {stuck_level}")

                        # [NEW] _stream_and_track 사용 및 토큰 정보 수집
                        token_info = None
                        content_chunks = []
                        
                        for chunk in _stream_and_track(llm, hint_prompt, user_id, model_name):
                            if isinstance(chunk, dict):
                                # 토큰 정보 수신
                                token_info = chunk
                            else:
                                # 일반 콘텐츠
                                content_chunks.append(chunk)
                                yield chunk
                        
                        # 🔥 [NEW] 토큰 소모 정보 전송 (프론트 표시용)
                        if token_info and token_info.get('cost', 0) > 0:
                            token_data = {
                                "type": "token_usage",
                                "tokens_used": token_info.get('tokens_used', 0),
                                "cost": token_info.get('cost', 0),
                                "model": token_info.get('model', 'unknown'),
                                "action": "hint_mode"
                            }
                            yield f"__TOKEN_INFO__{json.dumps(token_data)}__"
                        
                        return
                    except Exception as e:
                        logger.error(f"Hint mode generation error: {e}")
                        # 폴백
                        yield "주변을 둘러보니 여러 가지 시도해볼 수 있을 것 같습니다."
                        return
        # =============================================================================




    # [MODE 2] 씬 변경됨 -> 장면 묘사
    # =============================================================================
    
    # [FLICKER FIX] 배경과 NPC 등장을 하나의 HTML 덩어리로 묶어서 전송
    prefix_html_buffer = ""

    # [NEW] 배경 이미지 출력 (MinIO)
    if curr_scene:
        background_image = curr_scene.get('background_image', '')
        if background_image:
            minio_bg_url = get_minio_url('backgrounds', background_image)
            prefix_html_buffer += f"""
            <div class="scene-background mb-4 rounded-lg overflow-hidden border border-gray-700 shadow-lg relative bg-gray-900" style="min-height: 12rem;">
                <img src="{minio_bg_url}" alt="background" class="w-full h-48 object-cover object-center scale-in block" style="display: block;">
            </div>
            """

    scene_desc = curr_scene.get('description', '')  # <--- scene_desc 변수 선언 추가

    npc_intro = check_npc_appearance(state)
    if npc_intro: 
        prefix_html_buffer += npc_intro

    # 버퍼에 내용이 있으면 한 번에 전송
    if prefix_html_buffer:
        # [FLICKER FIX] 이미지 전송 (스팬 제거 - JS가 처리함)
        yield f"__PREFIX_START__{prefix_html_buffer}__PREFIX_END__"

    # YAML에서 씬 묘사 프롬프트 로드
    npc_list = ', '.join(npc_names) if npc_names else '없음'
    prompts = load_player_prompts()
    scene_prompt_template = prompts.get('scene_description', '')

    if scene_prompt_template:
        player_status = format_player_status(scenario, state.get('player_vars', {}))

        # [추가] transitions 리스트 생성 - 장면 묘사에 포함할 선택지들
        transitions = curr_scene.get('transitions', [])
        available_transitions = ""
        if transitions:
            # 부정적 엔딩으로 가는 transition 제외
            filtered_transitions = filter_negative_transitions(transitions, scenario)
            if filtered_transitions:
                available_transitions = "\n".join([f"- {t.get('trigger', '')}" for t in filtered_transitions])
            else:
                available_transitions = "현재 특별한 선택지가 없습니다."
        else:
            available_transitions = "현재 특별한 선택지가 없습니다."

        # 씬 변경 시 유저 입력 컨텍스트 포함
        if user_input:
            context_prefix = f"""**최우선 지침: 유저의 마지막 입력("{user_input}")이 이 장면으로의 전환을 일으켰습니다. 그 결과를 먼저 서술하세요.**

"""
            # 작업 2: 죽은 NPC 상태 컨텍스트 주입
            prompt = npc_status_context + context_prefix + scene_prompt_template.format(
                player_status=player_status,
                scene_title=scene_title,
                scene_desc=scene_desc,
                npc_list=npc_list,
                available_transitions=available_transitions
            )
        else:
            # 작업 2: 죽은 NPC 상태 컨텍스트 주입
            prompt = npc_status_context + scene_prompt_template.format(
                player_status=player_status,
                scene_title=scene_title,
                scene_desc=scene_desc,
                npc_list=npc_list,
                available_transitions=available_transitions
            )
    else:
        # 폴백 프롬프트
        # 작업 2: 죽은 NPC 상태 컨텍스트 주입
        prompt = npc_status_context + f"""당신은 텍스트 기반 RPG의 게임 마스터입니다.

**장면 정보:**
- 제목: "{scene_title}"
- 설명: "{scene_desc}"
- 등장 NPC: {npc_list}


**이제 장면을 묘사하세요:**"""

    try:
        api_key = os.getenv("OPENROUTER_API_KEY")
        model_name = state.get('model', 'openai/tngtech/deepseek-r1t2-chimera:free')
        llm = get_cached_llm(api_key=api_key, model_name=model_name, streaming=True)

        accumulated_text = ""
        has_content = False

        # [NEW] _stream_and_track 사용 및 토큰 정보 수집
        token_info = None
        content_chunks = []
        
        for chunk in _stream_and_track(llm, prompt, user_id, model_name):
            if isinstance(chunk, dict):
                # 토큰 정보 수신
                token_info = chunk
            else:
                # 일반 콘텐츠
                content_chunks.append(chunk)
                accumulated_text += chunk
                has_content = True
                yield chunk
        
        # 🔥 [NEW] 토큰 소모 정보 전송 (프론트 표시용)
        if token_info and token_info.get('cost', 0) > 0:
            token_data = {
                "type": "token_usage",
                "tokens_used": token_info.get('tokens_used', 0),
                "cost": token_info.get('cost', 0),
                "model": token_info.get('model', 'unknown'),
                "action": "scene_description"
            }
            yield f"__TOKEN_INFO__{json.dumps(token_data)}__"

        if not has_content or len(accumulated_text.strip()) < 10:
            raise Exception("Empty or insufficient response from LLM")

    except Exception as e:
        logger.error(f"Scene Streaming Error (attempt {retry_count + 1}): {e}")

        if retry_count < max_retries:
            yield f"__RETRY_SIGNAL__"
            return

        fallback_msg = get_narrative_fallback_message(scenario)

        if scene_desc:
            yield f"""
            <div class="bg-yellow-900/30 border border-yellow-700/50 rounded-lg p-4 my-2">
                <div class="text-yellow-400 serif-font mb-2">{fallback_msg}</div>
            </div>
            <div class="text-gray-300 serif-font">{scene_desc}</div>
            """
        else:
            yield f"""
            <div class="bg-yellow-900/30 border border-yellow-700/50 rounded-lg p-4 my-2">
                <div class="text-yellow-400 serif-font">{fallback_msg}</div>
            </div>
            """


# --- Graph Construction ---

def create_game_graph():
    """
    LangGraph 워크플로우 생성
    intent_parser -> (rule_engine | npc_actor) -> narrator -> END
    """
    workflow = StateGraph(PlayerState)

    # 노드 추가
    workflow.add_node("intent_parser", intent_parser_node)
    workflow.add_node("rule_engine", rule_node)
    workflow.add_node("npc_actor", npc_node)
    workflow.add_node("narrator", narrator_node)

    # 시작점 설정
    workflow.set_entry_point("intent_parser")

    # 라우팅 함수: 의도에 따라 rule_engine 또는 npc_actor로 분기
    def route_action(state):
        intent = state.get('parsed_intent')
        # ✅ item_action 의도를 rule_engine으로 라우팅 추가
        if intent in ['transition', 'ending', 'investigate', 'attack', 'item_action']:
            return "rule_engine"
        else:
            return "npc_actor"

    # 조건부 엣지 추가
    workflow.add_conditional_edges(
        "intent_parser",
        route_action,
        {
            "rule_engine": "rule_engine",
            "npc_actor": "npc_actor"
        }
    )

    # 순차 엣지 추가
    workflow.add_edge("rule_engine", "narrator")
    workflow.add_edge("npc_actor", "narrator")
    workflow.add_edge("narrator", END)

    return workflow.compile()


# --- [NEW] Game Engine Wrapper for Token Management ---

class GameEngine:
    """
    게임 로직 실행 및 토큰 과금 관리 래퍼 클래스
    """

    def __init__(self):
        self.workflow = create_game_graph()

    def run_turn(self, user_id: str, current_state: Dict[str, Any], user_input: str) -> Dict[str, Any]:
        """
        한 턴을 진행하고 토큰 비용을 정산하는 메인 메서드 (Sync Logic)
        """
        # 1. 잔액 확인 (최소 비용 체크)
        current_balance = UserService.get_user_balance(user_id)
        if current_balance <= 0:
            return {
                "error": "토큰이 부족합니다. 충전 후 이용해주세요.",
                "balance": 0
            }

        # 입력 상태 준비
        inputs = current_state
        inputs['user_input'] = user_input
        # [NEW] user_id 주입 (state를 통해 전파)
        inputs['user_id'] = user_id

        # 모델명 (추후 유저 설정이나 시나리오 설정에서 로드)
        model_name = inputs.get('model', 'openai/google/gemini-2.0-flash-001')

        prompt_tokens = 0
        completion_tokens = 0
        final_state = None

        try:
            # [핵심] LangChain Callback으로 입/출력 토큰 분리 측정 (Intent Parser, NPC Chat 등)
            with get_openai_callback() as cb:
                # 실제 게임 그래프 실행 (Blocking)
                final_state = self.workflow.invoke(inputs)

                # 사용량 집계 (Callback에서 추출)
                prompt_tokens = cb.prompt_tokens
                completion_tokens = cb.completion_tokens

            # 3. 비용 계산 및 차감 (1K 단위 분리 계산)
            total_tokens = prompt_tokens + completion_tokens

            if total_tokens > 0:
                cost = UserService.calculate_llm_cost(model_name, prompt_tokens, completion_tokens)

                UserService.deduct_tokens(
                    user_id=user_id,
                    cost=cost,
                    action_type="game_turn",
                    model_name=model_name,
                    llm_tokens_used=total_tokens
                )

            # 결과 반환
            return final_state

        except ValueError as ve:
            logger.warning(f"Game turn interrupted: {ve}")
            return {"error": str(ve)}
        except Exception as e:
            logger.error(f"Game turn failed: {e}")
            return {"error": "게임 진행 중 오류가 발생했습니다."}
