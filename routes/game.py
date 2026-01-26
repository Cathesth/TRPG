import logging
import json
import traceback
from datetime import datetime
import asyncio
from langchain_core.messages import SystemMessage, HumanMessage
from fastapi import APIRouter, Request, Form, Depends, BackgroundTasks, Query
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.orm import Session

from core.state import GameState, WorldState as WorldStateManager
import game_engine
from routes.auth import get_current_user_optional, CurrentUser
from models import GameSession, get_db
from schemas import GameAction

logger = logging.getLogger(__name__)

game_router = APIRouter(prefix="/game", tags=["game"])

# 최대 재시도 횟수
MAX_RETRIES = 2


def enrich_world_state(world_state: dict, player_state: dict, scenario: dict = None,
                       db_session: GameSession = None) -> dict:
    """
    World State를 완전하게 보강하는 공통 함수

    Args:
        world_state: 원본 world_state 딕셔너리
        player_state: player_state 딕셔너리 (stuck_count 등 미러링용)
        scenario: 시나리오 데이터 (씬 타이틀 조회용)
        db_session: DB 세션 레코드 (current_scene_id, turn_count 조회용)

    Returns:
        보강된 world_state 딕셔너리
    """
    enriched = world_state.copy() if world_state else {}

    # 1. location 및 current_scene_id 동기화
    location = enriched.get('location') or player_state.get('current_scene_id') or (
        db_session.current_scene_id if db_session else '')
    enriched['location'] = location
    enriched['current_scene_id'] = location

    # 2. stuck_count 미러링 (player_state → world_state)
    stuck_count = enriched.get('stuck_count')
    if stuck_count is None:
        stuck_count = player_state.get('stuck_count', 0)
        enriched['stuck_count'] = stuck_count

    # 3. turn_count 보강 (world_state → db_session → 0)
    if 'turn_count' not in enriched or enriched['turn_count'] is None:
        if db_session and db_session.turn_count is not None:
            enriched['turn_count'] = db_session.turn_count
        else:
            enriched['turn_count'] = 0

    # 4. current_scene_title 주입 (시나리오에서 조회)
    if location and scenario:
        scenes = scenario.get('scenes', [])
        for scene in scenes:
            if scene.get('scene_id') == location:
                enriched['current_scene_title'] = scene.get('title') or scene.get('name', '')
                break

    # 5. current_scene_title이 여전히 없으면 빈 문자열
    if 'current_scene_title' not in enriched:
        enriched['current_scene_title'] = ''

    logger.info(
        f"[SESSION_STATE] Enriched world_state: location={enriched.get('location')}, "
        f"title={enriched.get('current_scene_title')}, stuck_count={enriched.get('stuck_count')}, "
        f"turn_count={enriched.get('turn_count')}"
    )

    return enriched


def enrich_inventory(player_vars: dict, scenario: dict) -> dict:
    """
    인벤토리 아이템을 상세 정보(이미지 포함)로 변환
    """
    enriched = player_vars.copy() if player_vars else {}
    inventory = enriched.get('inventory', [])
    
    if not inventory:
        return enriched

    # 시나리오 아이템 데이터 매핑 (name -> data)
    scenario_items = {}
    
    # [FIX] raw_graph 내의 items도 검색 (시나리오 구조에 따라 items 위치가 다를 수 있음)
    if scenario and 'raw_graph' in scenario and 'items' in scenario['raw_graph']:
        for item in scenario['raw_graph']['items']:
            if isinstance(item, dict) and 'name' in item:
                item_name = item['name'].strip()
                # 이미 있으면(최상위 items 우선), raw_graph 것은 덮어쓰지 않거나 병합
                # 여기서는 raw_graph에만 이미지 정보가 있을 수도 있으므로, 없는 필드만 보강하도록 처리
                if item_name not in scenario_items:
                    scenario_items[item_name] = item
                else:
                    # 기존 정보에 이미지가 없으면 raw_graph 정보 사용
                    if 'image' not in scenario_items[item_name] and 'image' in item:
                        scenario_items[item_name]['image'] = item['image']

    # [FIX] raw_graph 내의 nodes(씬/엔딩 등)에 정의된 items도 검색 (이미지가 여기에만 숨어있는 경우 대응)
    if scenario and 'raw_graph' in scenario and 'nodes' in scenario['raw_graph']:
        for node in scenario['raw_graph']['nodes']:
            if 'data' in node and 'items' in node['data']:
                for item in node['data']['items']:
                    if isinstance(item, dict) and 'name' in item:
                        item_name = item['name'].strip()
                        # 이미지 정보가 있는 경우에만 업데이트 시도
                        if 'image' in item and item['image']:
                            if item_name not in scenario_items:
                                scenario_items[item_name] = item
                            elif 'image' not in scenario_items[item_name]:
                                # 기존에 항목은 있지만 이미지가 없는 경우 업데이트
                                scenario_items[item_name]['image'] = item['image']

    enriched_inventory = []
    for item in inventory:
        # 이미 객체라면 스킵
        if isinstance(item, dict):
            enriched_inventory.append(item)
            continue
            
        item_name = str(item)
        item_data = {'name': item_name}
        
        # 상세 정보 병합
        if item_name in scenario_items:
            # 설명 등 기본 정보 복사 (image 필드가 있으면 덮어씌워짐)
            item_data.update(scenario_items[item_name])

        # [MOVED] 이미지 필드가 명시적으로 있는 경우에만 경로 해결 (자동 생성 제거로 404 방지)
        if 'image' in item_data and item_data['image']:
            # [FIX] 모든 이미지를 get_minio_url로 통과시켜 내부망 URL(internal/localhost) 등을 프록시 경로로 변환
            # (이미 유효한 외부 URL은 그대로 반환됨)
            original_image = item_data['image']
            item_data['image'] = game_engine.get_minio_url('ai-images/item', original_image)
            logger.info(f"🖼️ [INVENTORY] Resolved scenario image URL for '{item_name}': {item_data['image']}")
        
        enriched_inventory.append(item_data)
        
    enriched['inventory'] = enriched_inventory
    return enriched


@game_router.get('/session_state')
async def get_session_state(
        session_id: str = Query(..., description="세션 ID"),
        db: Session = Depends(get_db),
        user: CurrentUser = Depends(get_current_user_optional)
):
    """
    프론트엔드가 서버의 최신 세션 상태를 조회하는 API
    디버그 패널 및 씬 보기 기능에서 사용
    ✅ [FIX 1-A] world_state를 enrich하여 항상 완전한 데이터 반환
    """
    try:
        game_session = db.query(GameSession).filter_by(session_key=session_id).first()

        if not game_session:
            return JSONResponse(
                status_code=404,
                content={
                    "success": False,
                    "error": "세션을 찾을 수 없습니다."
                }
            )

        # ✅ [FIX 1-A] 시나리오 데이터 로드 (씬 타이틀 조회용)
        scenario = None
        if game_session.scenario_id:
            scenario = game_engine.get_scenario_by_id(game_session.scenario_id)

        # ✅ [FIX 1-A] world_state 보강
        enriched_world_state = enrich_world_state(
            world_state=game_session.world_state or {},
            player_state=game_session.player_state or {},
            scenario=scenario,
            db_session=game_session
        )

        # [FIX] inventory Enrich (이미지 처리)
        player_state = game_session.player_state.copy() if game_session.player_state else {}
        if 'player_vars' in player_state:
             player_state['player_vars'] = enrich_inventory(player_state['player_vars'], scenario)

        # player_state와 world_state를 함께 반환
        return JSONResponse(content={
            "success": True,
            "session_id": game_session.session_key,
            "scenario_id": game_session.scenario_id,
            "player_state": player_state,
            "world_state": enriched_world_state,  # ✅ 보강된 world_state
            "turn_count": game_session.turn_count,
            "current_scene_id": game_session.current_scene_id,
            "last_played_at": game_session.last_played_at.isoformat() if game_session.last_played_at else None
        })

    except Exception as e:
        logger.error(f"❌ [API] Failed to get session state: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": str(e)
            }
        )


async def save_to_redis_async(session_key: str, cache_data: dict):
    """Redis에 비동기로 저장하는 헬퍼 함수"""
    try:
        from core.redis_client import get_redis_client
        redis_client = await get_redis_client()
        if redis_client.is_connected:
            await redis_client.set(f"session:{session_key}", cache_data, expire=3600)
            logger.info(f"✅ [REDIS] Session cached: {session_key}")
    except Exception as e:
        logger.warning(f"⚠️ [REDIS] Cache save failed: {e}")


def save_game_session(db: Session, state: dict, user_id: str = None, session_key: str = None):
    """
    🛠️ WorldState를 DB에 영속적으로 저장 (경량화 버전)

    Args:
        db: DB 세션
        state: PlayerState 딕셔너리
        user_id: 유저 ID (비로그인은 None)
        session_key: 세션 키 (없으면 신규 생성)

    Returns:
        session_key: 세션 키
    """
    try:
        # [경량화] scenario 전체가 아닌 scenario_id만 사용
        scenario_id = state.get('scenario_id', 0)
        current_scene_id = state.get('current_scene_id', '')

        # ✅ [FIX 1-1] 원본 state를 mutate하지 않도록 deepcopy 사용
        import copy
        world_state_data = copy.deepcopy(state.get('world_state', {}))

        # 저장용 state는 world_state 제외 (deepcopy로 원본 보호)
        state_for_db = copy.deepcopy(state)
        state_for_db.pop('world_state', None)

        # ✅ [B-2] world_state가 없어도 빈 인스턴스를 만들지 않음 (데이터 손실 방지)
        if not world_state_data:
            # 빈 dict로 유지하고 새 인스턴스 생성 금지
            world_state_data = {}
            logger.warning(f"⚠️ [DB SAVE] world_state is empty - saving empty dict (no new instance created)")

        # ✅ [B-2] location 동기화는 dict 부분 수정만 허용 (world_state_data가 있을 때만)
        if isinstance(world_state_data, dict) and current_scene_id:
            world_state_data['location'] = current_scene_id
            logger.info(f"🔧 [DB SAVE] Synced world_state.location = {current_scene_id}")

        # ✅ [FIX 1-B] stuck_count를 world_state에 미러링 (player_state → world_state)
        if isinstance(world_state_data, dict):
            stuck_count_from_state = state.get('stuck_count', 0)
            world_state_data['stuck_count'] = stuck_count_from_state
            logger.info(f"🔧 [DB SAVE] Mirrored stuck_count to world_state: {stuck_count_from_state}")

        turn_count = world_state_data.get('turn_count', 0) if isinstance(world_state_data, dict) else 0

        if session_key:
            # 기존 세션 업데이트
            game_session = db.query(GameSession).filter_by(session_key=session_key).first()
            if game_session:
                game_session.player_state = state_for_db  # world_state 제외된 경량화된 상태
                game_session.world_state = world_state_data  # 별도 컬럼에 저장
                game_session.current_scene_id = current_scene_id
                game_session.turn_count = turn_count
                game_session.last_played_at = datetime.now()
                game_session.updated_at = datetime.now()
                db.commit()
                logger.info(f"✅ [DB] Game session updated: {session_key}")

                return session_key
            else:
                logger.warning(f"⚠️ [DB] Session key provided but not found, creating new: {session_key}")

        # 신규 세션 생성
        import uuid
        new_session_key = session_key if session_key else str(uuid.uuid4())

        game_session = GameSession(
            user_id=user_id,
            session_key=new_session_key,
            scenario_id=scenario_id,
            player_state=state_for_db,  # world_state 제외된 경량화된 상태
            world_state=world_state_data,  # 별도 컬럼에 저장
            current_scene_id=current_scene_id,
            turn_count=turn_count
        )

        db.add(game_session)
        db.commit()
        logger.info(f"✅ [DB] New game session created: {new_session_key}")

        return new_session_key

    except Exception as e:
        logger.error(f"❌ [DB] Failed to save game session: {e}")
        db.rollback()
        return session_key  # 실패 시 기존 세션 키 반환


def load_game_session(db: Session, session_key: str):
    """
    🛠️ DB에서 WorldState 복원 (경량화 버전)

    Args:
        db: DB 세션
        session_key: 세션 키

    Returns:
        PlayerState 딕셔너리 또는 None
    """
    try:
        game_session = db.query(GameSession).filter_by(session_key=session_key).first()

        if not game_session:
            logger.warning(f"⚠️ [DB] Game session not found: {session_key}")
            return None

        # WorldState 복원 (싱글톤 인스턴스에 로드)
        wsm = WorldStateManager()
        wsm.from_dict(game_session.world_state)

        # [경량화] PlayerState는 world_state를 포함하지 않음
        player_state = game_session.player_state

        # ✅ [작업 1] DB에서 로드한 current_scene_id가 최신 값인지 검증
        db_scene_id = game_session.current_scene_id
        state_scene_id = player_state.get('current_scene_id', '')
        ws_location = game_session.world_state.get('location', '')

        # 우선순위: DB의 current_scene_id > world_state.location > player_state.current_scene_id
        verified_scene_id = db_scene_id or ws_location or state_scene_id

        if db_scene_id != state_scene_id or db_scene_id != ws_location:
            logger.warning(
                f"⚠️ [DB LOAD] Scene ID mismatch detected! "
                f"DB: {db_scene_id}, PlayerState: {state_scene_id}, WorldState: {ws_location}"
            )
            logger.info(f"🔧 [DB LOAD] Using verified scene_id: {verified_scene_id}")

        # player_state의 current_scene_id를 검증된 값으로 강제 업데이트
        player_state['current_scene_id'] = verified_scene_id
        wsm.location = verified_scene_id

        # ✅ [FIX] world_state를 player_state에 포함시켜 game_engine이 초기화하지 않도록 함
        if game_session.world_state:
            player_state['world_state'] = game_session.world_state
            logger.info(f"🌍 [DB LOAD] world_state included in player_state (location: {verified_scene_id})")

        logger.info(
            f"✅ [DB] Game session loaded: {session_key} "
            f"(Turn: {game_session.turn_count}, Scene: {verified_scene_id})"
        )

        return player_state

    except Exception as e:
        logger.error(f"❌ [DB] Failed to load game session: {e}")
        return None


@game_router.post('/act')
async def game_act():
    """HTMX Fallback (사용 안함)"""
    return "Please use streaming mode."


@game_router.post('/act_stream')
async def game_act_stream(
        request: Request,
        background_tasks: BackgroundTasks,
        user: CurrentUser = Depends(get_current_user_optional),
        db: Session = Depends(get_db)
):
    """스트리밍 방식 - SSE (LangGraph 기반) + WorldState DB 영속성 + 세션/시나리오 정합성 검증"""

    # [수정] JSON 요청으로 데이터 읽기
    try:
        json_body = await request.json()
        action = json_body.get('action', '').strip()
        session_id = json_body.get('session_id')
        scenario_id = json_body.get('scenario_id')  # ✅ 추가: 클라이언트에서 보낸 scenario_id
        model = json_body.get('model', 'openai/tngtech/deepseek-r1t2-chimera:free')
        provider = json_body.get('provider', 'deepseek')
    except:
        # JSON 파싱 실패 시 에러 반환
        def error_gen():
            yield f"data: {json.dumps({'type': 'error', 'content': 'Invalid request format'})}\n\n"

        return StreamingResponse(error_gen(), media_type='text/event-stream')

    # ✅ [중요] 세션 ID와 시나리오 ID 검증 로직
    should_create_new_session = False

    # 🔍 [SESSION ISOLATION] 세션별 독립적인 GameState 인스턴스 생성
    game_state = GameState()
    logger.info(f"🔍 [SESSION ISOLATION] Created local GameState instance for session: {session_id or 'new'}")

    if session_id:
        logger.info(f"🔍 [SESSION] Client provided session_id: {session_id}, scenario_id: {scenario_id}")

        # DB에서 세션 복구 시도
        game_session_record = db.query(GameSession).filter_by(session_key=session_id).first()

        if game_session_record:
            # ✅ [중요] 세션의 scenario_id와 요청받은 scenario_id 일치 여부 검증
            stored_scenario_id = game_session_record.scenario_id

            # ✅ [FIX] 타입 불일치 방지: 양쪽 모두 int()로 형변환하여 비교
            if scenario_id is not None and int(stored_scenario_id) != int(scenario_id):
                logger.warning(
                    f"⚠️ [SESSION MISMATCH] Session {session_id} has scenario_id={stored_scenario_id} (type: {type(stored_scenario_id).__name__}), "
                    f"but request has scenario_id={scenario_id} (type: {type(scenario_id).__name__}). Creating new session."
                )
                should_create_new_session = True
                session_id = None  # 세션 무효화
            else:
                # ✅ 시나리오 일치 확인됨 - 세션 복구
                restored_state = load_game_session(db, session_id)

                if restored_state:
                    # ✅ DB에서 복구한 세션으로 로컬 game_state에 설정
                    game_state.state = restored_state

                    # game_graph도 생성 - game_engine 모듈 사용
                    game_state.game_graph = game_engine.create_game_graph()

                    # ✅ [수정 1] 로컬 WorldState 인스턴스는 복원만 하고 덮어쓰지 않음
                    wsm = WorldStateManager()
                    if 'world_state' in restored_state:
                        wsm.from_dict(restored_state['world_state'])
                        turn_count = restored_state.get('world_state', {}).get('turn_count', 0)
                        logger.info(
                            f"🔍 [SESSION ISOLATION] Restored WorldState for session: {session_id}, turn: {turn_count}")
                    else:
                        logger.warning(f"⚠️ [WORLD INIT] world_state missing in restored_state")

                    logger.info(f"✅ [SESSION RESTORE] Session restored from DB: {session_id}")
                else:
                    logger.warning(f"⚠️ [SESSION] Failed to load state for session: {session_id}")
                    should_create_new_session = True
                    session_id = None
        else:
            logger.warning(f"⚠️ [SESSION] Session ID {session_id} not found in DB")
            should_create_new_session = True
            session_id = None
    else:
        # 세션 ID가 없으면 새로 생성
        logger.info(f"🆕 [SESSION] No session_id provided, will create new session")
        should_create_new_session = True

    # ✅ 세션이 무효화된 경우 에러 반환 (클라이언트가 시나리오를 다시 로드하도록)
    if should_create_new_session and not session_id:
        if not game_state.state or not game_state.game_graph:
            def error_gen():
                yield f"data: {json.dumps({'type': 'error', 'content': '세션을 찾을 수 없습니다. 시나리오를 다시 로드해주세요.'})}\n\n"

            return StreamingResponse(error_gen(), media_type='text/event-stream')

    if not game_state.state or not game_state.game_graph:
        def error_gen():
            yield f"data: {json.dumps({'type': 'error', 'content': '먼저 게임을 로드해주세요.'})}\n\n"

        return StreamingResponse(error_gen(), media_type='text/event-stream')

    action_text = action
    current_state = game_state.state

    # ✅ 작업 4: 클라이언트가 보낸 model 값이 우선순위를 가짐 (시나리오 기본 모델보다 우선)
    if model:
        current_state['model'] = model
        logger.info(f"🤖 [MODEL OVERRIDE] Using client-specified model: {model}")
    elif 'model' not in current_state or not current_state.get('model'):
        # 클라이언트가 model을 지정하지 않았고 state에도 없으면 기본값 사용
        current_state['model'] = 'openai/tngtech/deepseek-r1t2-chimera:free'
        logger.info(f"🤖 [MODEL DEFAULT] Using default model")

    # 1. 사용자 입력 저장
    current_state['last_user_input'] = action_text
    current_state['last_user_choice_idx'] = -1

    # 2. 게임 시작 여부 판단
    is_game_start = (
            action_text.lower() in ['시작', 'start', '게임시작'] and
            current_state.get('system_message') in ['Loaded', 'Init']
    )

    async def generate():
        nonlocal session_id

        try:
            processed_state = current_state

            # [FIX] scenario_id로 시나리오 조회
            scenario_id = current_state.get('scenario_id')
            if not scenario_id:
                yield f"data: {json.dumps({'type': 'error', 'content': '시나리오 ID가 없습니다.'})}\n\n"
                return

            scenario = game_engine.get_scenario_by_id(scenario_id)
            if not scenario:
                yield f"data: {json.dumps({'type': 'error', 'content': '시나리오를 찾을 수 없습니다.'})}\n\n"
                return

            if is_game_start:
                # ✅ [수정 2] 게임 시작 시에만 WorldState 초기화
                if not session_id:
                    # 새 게임: WorldState 초기화
                    wsm = WorldStateManager()
                    wsm.reset()
                    wsm.initialize_from_scenario(scenario)
                    logger.info(f"🎮 [GAME START] New game - WorldState initialized")

                    # 초기화된 world_state를 processed_state에 설정
                    processed_state['world_state'] = wsm.to_dict()
                else:
                    # 기존 세션 재개: world_state가 이미 processed_state에 있으면 그대로 사용
                    logger.info(f"🎮 [GAME START] Resuming existing session: {session_id}")
                    if 'world_state' not in processed_state:
                        logger.warning(f"⚠️ [GAME START] world_state missing in resumed session")

                start_scene_id = current_state.get('start_scene_id') or current_state.get('current_scene_id')

                # [추가] start_scene_id가 prologue인 경우 보정
                if start_scene_id == 'prologue':
                    actual_start_scene_id = scenario.get('start_scene_id')
                    if not actual_start_scene_id:
                        scenes = scenario.get('scenes', [])
                        if scenes:
                            actual_start_scene_id = scenes[0].get('scene_id', 'Scene-1')
                        else:
                            actual_start_scene_id = 'Scene-1'
                    start_scene_id = actual_start_scene_id
                    logger.info(f"🔧 [GAME START] Corrected prologue -> {start_scene_id}")

                logger.info(f"🎮 [GAME START] Start Scene: {start_scene_id}")
                current_state['current_scene_id'] = start_scene_id
                current_state['system_message'] = 'Game Started'
                current_state['is_game_start'] = True

                # location 동기화 (world_state가 있는 경우에만)
                if 'world_state' in processed_state and isinstance(processed_state['world_state'], dict):
                    processed_state['world_state']['location'] = start_scene_id
                    logger.info(f"🔧 [GAME START] Synced world_state.location = {start_scene_id}")
            else:
                # ✅ [수정 2-핵심] 일반 턴: LangGraph가 world_state를 생성/갱신
                logger.info(f"🎮 Action: {action_text}")
                current_state['is_game_start'] = False

                # LangGraph invoke - 이미 world_state를 포함하고 있음
                processed_state = game_state.game_graph.invoke(current_state)
                game_state.state = processed_state

                # ✅ [치명적 버그 수정] LangGraph가 생성한 world_state를 절대 덮어쓰지 않음
                # processed_state에 이미 world_state가 있으면 그대로 사용
                if 'world_state' in processed_state:
                    logger.info(
                        f"✅ [WORLD STATE] Using LangGraph-generated world_state (turn: {processed_state.get('world_state', {}).get('turn_count', 'N/A')})")
                else:
                    logger.warning(f"⚠️ [WORLD STATE] LangGraph did not return world_state!")

            # ✅ [수정 3] 검증이 필요한 경우에만 복원해서 읽기만 함 (덮어쓰지 않음)
            if 'world_state' in processed_state and isinstance(processed_state['world_state'], dict):
                # 검증용 로그
                ws_turn = processed_state['world_state'].get('turn_count', 0)
                ws_location = processed_state['world_state'].get('location', 'unknown')
                logger.info(f"🌍 [WORLD STATE VERIFY] turn_count={ws_turn}, location={ws_location}")
            else:
                logger.error(f"❌ [WORLD STATE] Missing or invalid world_state in processed_state!")

            # ✅ [MOVED] 전투 묘사 트리거 처리 (API 레벨에서 비동기 LLM 호출 - DB 저장 전 처리)
            # [DEBUG] Processed State 검사
            internal_flags = processed_state.get('_internal_flags', {})
            has_trigger = 'combat_desc_trigger' in internal_flags
            logger.info(f"🕵️ [DEBUG] processed_state keys: {list(processed_state.keys())}, Internal Flags keys: {list(internal_flags.keys())}, Has Trigger: {has_trigger}")

            combat_trigger = internal_flags.get('combat_desc_trigger')
            if combat_trigger:
                logger.info(f"✨ [API] Trigger Found! Threshold: {combat_trigger.get('threshold')}")
                try:
                    from llm_factory import LLMFactory
                    logger.info("🛠️ [API] Importing LLMFactory success")
                    
                    # [DEBUG] LLM 생성 로그
                    model_name = "google/gemini-2.0-flash-001"
                    logger.info(f"🛠️ [API] Creating LLM: {model_name}")
                    
                    llm = LLMFactory.get_llm(model_name)
                    logger.info(f"✅ [API] LLM Created: {type(llm)}")
                    desc_prompt = f"""
                    [TRPG 전투 상황]
                    적: {combat_trigger.get('npc_name')} ({combat_trigger.get('npc_type')})
                    특징: {combat_trigger.get('npc_desc')}
                    플레이어 행동: {combat_trigger.get('user_input')}
                    상황: 체력이 {int(combat_trigger.get('threshold', 0) * 100)}% 이하로 떨어졌습니다!
                    
                    이 긴박한 순간의 적의 반응이나 파손 상태를 1문장으로 생동감 있게 묘사하세요. (문학적 표현 사용)
                    """
                    
                    # Async generation
                    messages = [
                        SystemMessage(content="당신은 TRPG 전투 내레이터입니다."),
                        HumanMessage(content=desc_prompt)
                    ]
                    response = await llm.ainvoke(messages)
                    llm_desc = response.content

                    # [LOGGING] 전투 묘사 로그 출력 (User Request)
                    logger.info(f"⚔️ [COMBAT DESC] Generated: {llm_desc}")
                    
                    if llm_desc:
                         # 묘사를 별도 메시지로 전송 (강조 스타일 적용)
                        desc_html = f"<div class='text-gray-300 italic mb-4 p-3 border-l-4 border-red-800 bg-red-900/20 font-serif leading-relaxed'>{llm_desc.strip()}</div>"
                        yield f"data: {json.dumps({'type': 'prefix', 'content': desc_html})}\n\n"
                        
                        # ✅ [PERSISTENCE] DB 저장을 위해 narrator_output에 추가
                        current_narrative = processed_state.get('narrator_output', '')
                        processed_state['narrator_output'] = current_narrative + f"\n\n[전투 묘사] {llm_desc.strip()}"
                        
                        # ✅ [PERSISTENCE] WorldState History에도 추가
                        if 'world_state' in processed_state:
                            ws_dict = processed_state['world_state']
                            if 'narrative_history' in ws_dict:
                                ws_dict['narrative_history'].append(f"전투 묘사: {llm_desc.strip()}")
                        
                        # Trigger 소비
                        processed_state.pop('combat_desc_trigger', None)
                except Exception as e:
                    logger.error(f"❌ [API] Combat Desc Generation Failed: {e}")

            # 🛠️ WorldState DB 저장
            user_id = user.id if user else None

            # ✅ 작업 4: 첫 턴(세션이 DB에 없을 때)에만 최초 저장, 이후 매 턴마다 업데이트
            if not session_id:
                # ✅ 첫 턴: DB에 세션이 없으므로 새로 생성
                session_id = save_game_session(db, processed_state, user_id, None)
                logger.info(f"✅ [FIRST TURN] Created new session in DB: {session_id}")
            else:
                # ✅ DB에서 세션 존재 여부 확인
                existing_session = db.query(GameSession).filter_by(session_key=session_id).first()

                if not existing_session:
                    # ✅ 클라이언트가 session_id를 보냈지만 DB에 없는 경우 (load_scenario 직후)
                    # 이 경우에만 최초 저장 수행
                    session_id = save_game_session(db, processed_state, user_id, session_id)
                    logger.info(f"✅ [FIRST TURN AFTER LOAD] Created session in DB with provided key: {session_id}")
                else:
                    # ✅ 일반 턴: 기존 세션 업데이트
                    session_id = save_game_session(db, processed_state, user_id, session_id)
                    logger.info(f"✅ [SESSION UPDATE] Updated existing session: {session_id}")

            # ✅ [작업 1] Redis 저장을 background_tasks로 비동기 처리
            cache_data = {
                'player_state': processed_state,
                'world_state': processed_state.get('world_state'),
                'current_scene_id': processed_state.get('current_scene_id'),
                'turn_count': processed_state.get('world_state', {}).get('turn_count', 0) if isinstance(
                    processed_state.get('world_state'), dict) else 0,
                'scenario_id': scenario_id
            }
            background_tasks.add_task(save_to_redis_async, session_id, cache_data)

            # 결과 추출
            npc_say = processed_state.get('npc_output', '')
            sys_msg = processed_state.get('system_message', '')
            intent = processed_state.get('parsed_intent')
            # [FIX] 엔딩 조건 보강 (씬 ID가 Ending으로 시작하면 엔딩으로 간주)
            current_scene_id = processed_state.get('current_scene_id', '')
            is_ending = (intent == 'ending') or (current_scene_id and (current_scene_id.startswith('Ending') or current_scene_id.startswith('ending')))

            # --- [스트리밍 응답 전송] ---



            # ✅ [중요] 세션 ID 전송 (프론트엔드에서 저장)
            if session_id:
                yield f"data: {json.dumps({'type': 'session_id', 'content': session_id})}\n\n"

            # A. 시스템 메시지
            if sys_msg and "Game Started" not in sys_msg:
                sys_html = f"<div class='text-xs text-indigo-400 mb-2 border-l-2 border-indigo-500 pl-2'>🚀 {sys_msg}</div>"
                yield f"data: {json.dumps({'type': 'prefix', 'content': sys_html})}\n\n"

            # B. NPC 대화 (NPC 이름 및 초상화 표시)
            if npc_say:
                curr_scene_id = processed_state['current_scene_id']
                all_scenes = {s['scene_id']: s for s in scenario.get('scenes', [])}
                curr_scene = all_scenes.get(curr_scene_id)
                npc_names = curr_scene.get('npcs', []) if curr_scene else []

                npc_name_str = "NPC"
                npc_image_url = ""

                # NPC 이름 및 이미지 URL 추출
                if npc_names:
                    first_npc = npc_names[0]
                    if isinstance(first_npc, dict):
                        npc_name_str = first_npc.get('name', 'NPC')
                        npc_image_url = first_npc.get('image', '')
                    else:
                        npc_name_str = first_npc

                # 이미지 태그 생성 (이미지가 있을 경우에만)
                img_tag = ""
                if npc_image_url:
                    import urllib.parse
                    # URL 안전하게 인코딩 (필요시)
                    safe_url = urllib.parse.quote(npc_image_url, safe=':/')
                    # 프록시 경로를 사용하거나 원본 URL 사용 (여기서는 프록시 경로 가정)
                    img_tag = f"""
                    <div class="w-12 h-12 rounded-none border-2 border-yellow-400 bg-rpg-900 overflow-hidden shrink-0 mr-3 shadow-md">
                        <img src="/image/serve/{safe_url}" class="w-full h-full object-cover pixel-avatar">
                    </div>
                    """

                npc_html = f"""
                <div class='bg-gradient-to-r from-yellow-900/30 to-yellow-800/20 p-4 rounded-lg border-l-4 border-yellow-500 mb-4 shadow-lg flex items-start'>
                    {img_tag}
                    <div class="flex-1">
                        <div class='flex items-center gap-2 mb-2'>
                            <i data-lucide="message-circle" class="w-4 h-4 text-yellow-400"></i>
                            <span class='text-yellow-400 font-bold text-sm uppercase tracking-wide'>{npc_name_str}</span>
                        </div>
                        <div class='text-gray-200 leading-relaxed pl-6'>{npc_say}</div>
                    </div>
                </div>
                """
                yield f"data: {json.dumps({'type': 'prefix', 'content': npc_html})}\n\n"

            # C. 프롤로그 (게임 시작 시)
            if is_game_start:
                prologue_text = scenario.get('prologue') or scenario.get('prologue_text', '')

                if prologue_text and prologue_text.strip():
                    prologue_html = '<div class="mb-6 p-4 bg-indigo-900/20 rounded-xl border border-indigo-500/30"><div class="text-indigo-400 font-bold text-sm mb-3 uppercase tracking-wider">[ Prologue ]</div><div class="text-gray-200 leading-relaxed serif-font text-lg">'
                    yield f"data: {json.dumps({'type': 'prefix', 'content': prologue_html})}\n\n"

                    for chunk in game_engine.prologue_stream_generator(processed_state):
                        yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"

                    yield f"data: {json.dumps({'type': 'section_end', 'content': '</div></div>'})}\n\n"
                    hr_content = '<hr class="border-gray-800 my-6">';
                    yield f"data: {json.dumps({'type': 'prefix', 'content': hr_content})}\n\n";

                # 프롤로그 후 첫 씬으로 이동
                prologue_connects_to = scenario.get('prologue_connects_to', [])
                if prologue_connects_to and len(prologue_connects_to) > 0:
                    first_scene_id = prologue_connects_to[0]
                else:
                    scenes = scenario.get('scenes', [])
                    first_scene_id = scenes[0]['scene_id'] if scenes else 'start'

                processed_state['current_scene_id'] = first_scene_id
                game_state.state = processed_state
                logger.info(f"🎮 [PROLOGUE -> SCENE] Moving to: {first_scene_id}")

                # 첫 씬 묘사 (재시도 로직 포함)
                for result in stream_scene_with_retry(processed_state):
                    yield result

            # D. 엔딩
            elif is_ending:
                ending_html = processed_state.get('narrator_output', '')
                yield f"data: {json.dumps({'type': 'ending_start', 'content': ending_html})}\n\n"
                yield f"data: {json.dumps({'type': 'game_ended', 'content': True})}\n\n"

            # E. 일반 씬 진행 (나레이션) - 재시도 로직 포함
            else:
                for result in stream_scene_with_retry(processed_state):
                    yield result

            # F. 스탯 업데이트 및 세션 키 전송
            player_vars = processed_state.get('player_vars', {})
            # [FIX] inventory Enrich (이미지 처리)
            stats_data = enrich_inventory(player_vars, scenario)
            yield f"data: {json.dumps({'type': 'stats', 'content': stats_data})}\n\n"

            # ✅ [수정 3] World State 전송 시 processed_state의 world_state를 그대로 사용
            world_state_data = processed_state.get('world_state', {})

            # 1-1. 배경 이미지 확인 및 전송
            current_loc = processed_state.get('current_scene_id')
            if current_loc:
                bg_image_url = ""
                
                # A. 시나리오 scenes/endings에서 검색
                search_list = scenario.get('scenes', [])
                if current_loc.startswith('Ending') or current_loc.startswith('ending'):
                     search_list += scenario.get('endings', [])
                
                for item in search_list:
                    # scene_id 또는 ending_id 매칭
                    if item.get('scene_id') == current_loc or item.get('ending_id') == current_loc:
                        # [FIX] Endings often use 'image' instead of 'background_image'
                        bg_image_url = item.get('background_image', '') or item.get('image', '') or item.get('image_prompt', '')
                        if bg_image_url:
                            # [FIX] URL resolution for internal/external paths
                            bg_image_url = game_engine.get_minio_url('bg', bg_image_url)
                        break
                
                # B. [FIX] raw_graph 내의 nodes에서도 검색 (누락 방지)
                if not bg_image_url and scenario and 'raw_graph' in scenario and 'nodes' in scenario['raw_graph']:
                    for node in scenario['raw_graph']['nodes']:
                         # Node ID가 current_loc와 일치(대소문자 무시)하거나, scene-id 매칭
                         node_id = node.get('id', '').lower()
                         target_id = current_loc.lower()
                         
                         # 매칭 조건: ID 일치 또는 data.scene_id/ending_id 일치
                         is_match = (node_id == target_id)
                         if not is_match and 'data' in node:
                             data_id = node['data'].get('scene_id') or node['data'].get('ending_id')
                             if data_id and data_id.lower() == target_id:
                                 is_match = True
                                 
                         if is_match and 'data' in node:
                             bg_image_url = node['data'].get('background_image', '') or node['data'].get('image', '')
                             if bg_image_url:
                                 bg_image_url = game_engine.get_minio_url('bg', bg_image_url)
                                 break

                # 배경 이미지가 있으면 클라이언트로 전송
                if bg_image_url:
                    yield f"data: {json.dumps({'type': 'bg_update', 'content': bg_image_url})}\n\n"

            if world_state_data:
                # World State에 씬 정보 추가
                world_state_with_scene = world_state_data.copy()

                # [FIX] 현재 위치는 player_state의 current_scene_id를 우선적으로 사용 (더 정확함)
                location_scene_id = processed_state.get('current_scene_id') or world_state_with_scene.get('location',
                                                                                                          '')

                # 디버그 로그
                logger.info(
                    f"🗺️ [WORLD STATE] current_scene_id: {processed_state.get('current_scene_id')}, world_state location: {world_state_with_scene.get('location')}, using: {location_scene_id}")

                location_scene_title = ''

                # 시나리오에서 해당 씬의 title 또는 name 찾기
                if location_scene_id:
                    # Scenes + Endings 모두 검색
                    all_locations = scenario.get('scenes', []) + scenario.get('endings', [])
                    
                    for loc in all_locations:
                        # scene_id 또는 ending_id 매칭
                        current_id = loc.get('scene_id') or loc.get('ending_id')
                        if current_id == location_scene_id:
                            # title 필드가 있으면 사용, 없으면 name 필드 사용
                            location_scene_title = loc.get('title') or loc.get('name', '')
                            logger.info(
                                f"🗺️ [WORLD STATE] Found title/name for {location_scene_id}: {location_scene_title}")
                            break

                    # title을 못 찾은 경우 로그
                    if not location_scene_title:
                        logger.warning(f"⚠️ [WORLD STATE] No title/name found for scene_id: {location_scene_id}")

                # current_scene_id와 current_scene_title 명시적으로 설정
                world_state_with_scene['current_scene_id'] = location_scene_id
                world_state_with_scene['current_scene_title'] = location_scene_title

                # location 필드도 current_scene_id로 동기화
                world_state_with_scene['location'] = location_scene_id

                # [FIX] turn_count가 없는 경우 0으로 초기화
                if 'turn_count' not in world_state_with_scene:
                    world_state_with_scene['turn_count'] = 0

                # [추가] stuck_count를 world_state에 포함
                stuck_count_value = processed_state.get('stuck_count', 0)
                world_state_with_scene['stuck_count'] = stuck_count_value

                # 디버그: 전송되는 데이터 로그
                logger.info(
                    f"📤 [WORLD STATE] Sending: scene_id={world_state_with_scene['current_scene_id']}, "
                    f"title={world_state_with_scene['current_scene_title']}, "
                    f"stuck_count={stuck_count_value}")

                yield f"data: {json.dumps({'type': 'world_state', 'content': world_state_with_scene})}\n\n"

            # NPC 정보 전송 (WorldState에서 추출 + 시나리오 전체 NPC)
            curr_scene_id = processed_state.get('current_scene_id', '')

            # 시나리오의 모든 NPC 정보를 딕셔너리로 구성
            all_scenario_npcs = {}
            for npc in scenario.get('npcs', []):
                if isinstance(npc, dict) and 'name' in npc:
                    npc_name = npc['name']
                    all_scenario_npcs[npc_name] = {
                        'name': npc_name,
                        'role': npc.get('role', 'Unknown'),
                        'personality': npc.get('personality', '보통'),
                        'hp': npc.get('hp', 100),
                        'max_hp': npc.get('max_hp', 100),
                        'status': 'alive',
                        'relationship': 50,
                        'emotion': 'neutral',
                        'location': '알 수 없음',
                        'is_hostile': npc.get('isEnemy', False),
                        'image': npc.get('image', None)  # [추가] 이미지 속성
                    }

            # WorldState의 NPC 정보로 업데이트
            if world_state_data and 'npcs' in world_state_data:
                world_npcs = world_state_data['npcs']
                for npc_name, npc_state in world_npcs.items():
                    if npc_name in all_scenario_npcs:
                        # 기존 시나리오 정보에 WorldState 정보 덮어쓰기
                        all_scenario_npcs[npc_name].update({
                            'hp': npc_state.get('hp', all_scenario_npcs[npc_name]['hp']),
                            'max_hp': npc_state.get('max_hp', all_scenario_npcs[npc_name]['max_hp']),
                            'status': npc_state.get('status', 'alive'),
                            'relationship': npc_state.get('relationship', 50),
                            'emotion': npc_state.get('emotion', 'neutral'),
                            'location': npc_state.get('location', all_scenario_npcs[npc_name]['location']),
                            'is_hostile': npc_state.get('is_hostile', all_scenario_npcs[npc_name]['is_hostile'])
                        })
                    else:
                        # WorldState에만 있는 NPC (동적 생성된 NPC)
                        all_scenario_npcs[npc_name] = {
                            'name': npc_name,
                            'role': 'Unknown',
                            'personality': '보통',
                            'hp': npc_state.get('hp', 100),
                            'max_hp': npc_state.get('max_hp', 100),
                            'status': npc_state.get('status', 'alive'),
                            'relationship': npc_state.get('relationship', 50),
                            'emotion': npc_state.get('emotion', 'neutral'),
                            'location': npc_state.get('location', '알 수 없음'),
                            'is_hostile': npc_state.get('is_hostile', False),
                            'image': npc_state.get('image', None)
                        }

            # 현재 씬의 NPC 위치 정보 업데이트
            # [FIX] unhashable type: 'dict' 에러 수정 및 이미지 연동
            # [FIX] unhashable type: 'dict' 에러 수정 및 이미지 연동
            # [FIX] KeyError: 'scene_id' 방지 (scene_id가 없는 항목 필터링)
            all_scenes = {s.get('scene_id'): s for s in scenario.get('scenes', []) if s.get('scene_id')}
            for scene_id, scene in all_scenes.items():
                scene_title = scene.get('title', scene_id)
                # npcs와 enemies 리스트 합치기
                scene_entities = scene.get('npcs', []) + scene.get('enemies', [])

                for entity in scene_entities:
                    # entity가 dict면 name 추출, 문자열이면 그대로 사용
                    entity_name = entity.get('name') if isinstance(entity, dict) else entity

                    if entity_name in all_scenario_npcs:
                        if all_scenario_npcs[entity_name]['location'] == '알 수 없음':
                            all_scenario_npcs[entity_name]['location'] = scene_title

                        # [중요] 씬 데이터에 이미지가 있다면 상태 정보에 반영 (이미지 연동)
                        if isinstance(entity, dict) and entity.get('image'):
                            all_scenario_npcs[entity_name]['image'] = entity['image']

            # 전체 NPC 정보 전송
            if all_scenario_npcs:
                yield f"data: {json.dumps({'type': 'npc_status', 'content': all_scenario_npcs})}\n\n"

            # 🛠️ 세션 키 전송 (클라이언트가 다음 요청에 사용)
            if session_id:
                yield f"data: {json.dumps({'type': 'session_key', 'content': session_id})}\n\n"

            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as e:
            logger.error(f"Stream Error: {e}", exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )


def stream_scene_with_retry(state):
    """씬 스트리밍 with 재시도 로직"""
    retry_count = 0

    while retry_count <= MAX_RETRIES:
        buffer = ""
        need_retry = False

        for chunk in game_engine.scene_stream_generator(state, retry_count=retry_count, max_retries=MAX_RETRIES):
            # 재시도 신호 감지
            if "__RETRY_SIGNAL__" in chunk:
                need_retry = True
                break
            
            # [FIX] 프리픽스 마커 처리 (이미지 플리커링 방지)
            if "__PREFIX_START__" in chunk:
                content = chunk.replace("__PREFIX_START__", "").replace("__PREFIX_END__", "")
                if content.strip():
                    yield f"data: {json.dumps({'type': 'prefix', 'content': content})}\n\n"
                continue

            buffer += chunk
            yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"

        if need_retry:
            retry_count += 1
            if retry_count <= MAX_RETRIES:
                logger.info(f"🔄 [RETRY] Attempt {retry_count}/{MAX_RETRIES}")
                yield f"data: {json.dumps({'type': 'retry', 'attempt': retry_count, 'max': MAX_RETRIES})}\n\n"
            else:
                logger.warning(f"⚠️ [FALLBACK] Max retries exceeded")
                fallback_msg = game_engine.get_narrative_fallback_message(state.get('scenario', {}))
                fallback_html = f"""
                <div class="bg-yellow-900/30 border border-yellow-700/50 rounded-lg p-4 my-2">
                    <div class="text-yellow-400 serif-font">{fallback_msg}</div>
                </div>
                """
                yield f"data: {json.dumps({'type': 'fallback', 'content': fallback_html})}\n\n"
                break
        else:
            # 성공적으로 완료
            break


@game_router.get('/session/{session_key}')
async def get_game_session_data(
        session_key: str,
        db: Session = Depends(get_db)
):
    """
    🛠️ Railway DB에서 게임 세션 데이터 불러오기
    - Player Status, NPC Status, World State 포함
    """
    try:
        game_session = db.query(GameSession).filter_by(session_key=session_key).first()

        if not game_session:
            return JSONResponse({
                "success": False,
                "error": "세션을 찾을 수 없습니다."
            }, status_code=404)

        # 시나리오 정보 조회 (NPC 전체 정보 필요)
        scenario = game_engine.get_scenario_by_id(game_session.scenario_id)

        # 시나리오의 모든 NPC 정보를 딕셔너리로 구성
        all_scenario_npcs = {}
        if scenario:
            for npc in scenario.get('npcs', []):
                if isinstance(npc, dict) and 'name' in npc:
                    npc_name = npc['name']
                    all_scenario_npcs[npc_name] = {
                        'name': npc_name,
                        'role': npc.get('role', 'Unknown'),
                        'personality': npc.get('personality', '보통'),
                        'hp': npc.get('hp', 100),
                        'max_hp': npc.get('max_hp', 100),
                        'status': 'alive',
                        'relationship': 50,
                        'emotion': 'neutral',
                        'location': '알 수 없음',
                        'is_hostile': npc.get('isEnemy', False),
                        # [추가] 이미지 속성
                        'image': npc.get('image', None)
                    }

        # WorldState의 NPC 정보로 업데이트
        if game_session.world_state and 'npcs' in game_session.world_state:
            world_npcs = game_session.world_state['npcs']
            for npc_name, npc_state in world_npcs.items():
                if npc_name in all_scenario_npcs:
                    # 기존 시나리오 정보에 WorldState 정보 덮어쓰기
                    all_scenario_npcs[npc_name].update({
                        'hp': npc_state.get('hp', all_scenario_npcs[npc_name]['hp']),
                        'max_hp': npc_state.get('max_hp', all_scenario_npcs[npc_name]['max_hp']),
                        'status': npc_state.get('status', 'alive'),
                        'relationship': npc_state.get('relationship', 50),
                        'emotion': npc_state.get('emotion', 'neutral'),
                        'location': npc_state.get('location', all_scenario_npcs[npc_name]['location']),
                        'is_hostile': npc_state.get('is_hostile', all_scenario_npcs[npc_name]['is_hostile'])
                    })
                else:
                    # WorldState에만 있는 NPC (동적 생성된 NPC)
                    all_scenario_npcs[npc_name] = {
                        'name': npc_name,
                        'role': 'Unknown',
                        'personality': '보통',
                        'hp': npc_state.get('hp', 100),
                        'max_hp': npc_state.get('max_hp', 100),
                        'status': npc_state.get('status', 'alive'),
                        'relationship': npc_state.get('relationship', 50),
                        'emotion': npc_state.get('emotion', 'neutral'),
                        'location': npc_state.get('location', '알 수 없음'),
                        'is_hostile': npc_state.get('is_hostile', False),
                        'image': npc_state.get('image', None)
                    }

        # 현재 씬의 NPC 위치 정보 업데이트
        if scenario:
            all_scenes = {s['scene_id']: s for s in scenario.get('scenes', [])}
            for scene_id, scene in all_scenes.items():
                scene_title = scene.get('title', scene_id)
                # [FIX] unhashable type: 'dict' 해결
                scene_entities = scene.get('npcs', []) + scene.get('enemies', [])

                for entity in scene_entities:
                    entity_name = entity.get('name') if isinstance(entity, dict) else entity

                    if entity_name in all_scenario_npcs:
                        if all_scenario_npcs[entity_name]['location'] == '알 수 없음':
                            all_scenario_npcs[entity_name]['location'] = scene_title

                        # [중요] 씬 데이터에 이미지가 있다면 상태 정보에 반영 (이미지 연동)
                        if isinstance(entity, dict) and entity.get('image'):
                            all_scenario_npcs[entity_name]['image'] = entity['image']

        # World State에 씬 정보 추가
        world_state_with_scene = game_session.world_state.copy() if game_session.world_state else {}

        # ✅ FIX: world_state.location을 player_state.current_scene_id와 동기화
        # DB에서 복원 시 location이 제대로 업데이트되지 않는 문제 해결
        player_current_scene = game_session.player_state.get('current_scene_id') if game_session.player_state else None
        db_current_scene = game_session.current_scene_id

        # 우선순위: player_state.current_scene_id > DB current_scene_id > world_state.location
        location_scene_id = player_current_scene or db_current_scene or world_state_with_scene.get('location')

        # world_state.location을 최신 위치로 강제 동기화
        world_state_with_scene['location'] = location_scene_id

        # ✅ [작업 2] 세션 조회 API 데이터 정합성 보강 - player_state의 데이터를 world_state에 강제 덮어쓰기
        world_state_with_scene['location'] = game_session.current_scene_id
        world_state_with_scene['stuck_count'] = game_session.player_state.get('stuck_count',
                                                                              0) if game_session.player_state else 0
        world_state_with_scene['turn_count'] = game_session.turn_count

        location_scene_title = ''

        # 시나리오에서 해당 씬의 title 또는 name 찾기
        if location_scene_id and scenario:
            for scene in scenario.get('scenes', []):
                if scene.get('scene_id') == location_scene_id:
                    location_scene_title = scene.get('title') or scene.get('name', '')
                    break

        # current_scene_id와 current_scene_title 명시적으로 설정
        world_state_with_scene['current_scene_id'] = location_scene_id
        world_state_with_scene['current_scene_title'] = location_scene_title

        # turn_count가 없는 경우 0으로 초기화
        if 'turn_count' not in world_state_with_scene:
            world_state_with_scene['turn_count'] = 0

        return JSONResponse({
            "success": True,
            "player_state": game_session.player_state,
            "world_state": world_state_with_scene,
            "npc_status": all_scenario_npcs,
            "current_scene_id": game_session.current_scene_id,
            "turn_count": game_session.turn_count,
            "last_played_at": game_session.last_played_at.isoformat() if game_session.last_played_at else None
        })

    except Exception as e:
        logger.error(f"❌ [DB] Failed to fetch game session: {e}")
        return JSONResponse({
            "success": False,
            "error": str(e)
        }, status_code=500)