import logging
import json
import traceback
from datetime import datetime
from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.orm import Session

from core.state import game_state, WorldState as WorldStateManager
from game_engine import scene_stream_generator, prologue_stream_generator, get_narrative_fallback_message, \
    get_scenario_by_id
from routes.auth import get_current_user_optional, CurrentUser
from models import GameSession, get_db
from schemas import GameAction

logger = logging.getLogger(__name__)

game_router = APIRouter(prefix="/game", tags=["game"])

# 최대 재시도 횟수
MAX_RETRIES = 2


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

        # [경량화] world_state는 별도 추출 (player_state에서 제거)
        world_state_data = state.pop('world_state', {})

        # WorldState 인스턴스에서 직접 가져오기
        if not world_state_data:
            wsm = WorldStateManager()
            world_state_data = wsm.to_dict()

        turn_count = world_state_data.get('turn_count', 0) if isinstance(world_state_data, dict) else 0

        if session_key:
            # 기존 세션 업데이트
            game_session = db.query(GameSession).filter_by(session_key=session_key).first()
            if game_session:
                game_session.player_state = state  # world_state 제외된 경량화된 상태
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
            player_state=state,  # world_state 제외된 경량화된 상태
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

        logger.info(f"✅ [DB] Game session loaded: {session_key} (Turn: {game_session.turn_count})")

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

    if session_id:
        logger.info(f"🔍 [SESSION] Client provided session_id: {session_id}, scenario_id: {scenario_id}")

        # DB에서 세션 복구 시도
        game_session_record = db.query(GameSession).filter_by(session_key=session_id).first()

        if game_session_record:
            # ✅ [중요] 세션의 scenario_id와 요청받은 scenario_id 일치 여부 검증
            stored_scenario_id = game_session_record.scenario_id

            if scenario_id is not None and stored_scenario_id != scenario_id:
                logger.warning(
                    f"⚠️ [SESSION MISMATCH] Session {session_id} has scenario_id={stored_scenario_id}, "
                    f"but request has scenario_id={scenario_id}. Creating new session."
                )
                should_create_new_session = True
                session_id = None  # 세션 무효화
            else:
                # ✅ 시나리오 일치 확인됨 - 세션 복구
                restored_state = load_game_session(db, session_id)

                if restored_state:
                    # ✅ DB에서 복구한 세션으로 game_state 완전히 교체
                    game_state.state = restored_state

                    # WorldState도 복구
                    wsm = WorldStateManager()
                    if 'world_state' in restored_state:
                        wsm.from_dict(restored_state['world_state'])

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

    # 선택한 모델을 상태에 저장
    if model:
        current_state['model'] = model
        logger.info(f"🤖 Using model: {model}")

    # 1. 사용자 입력 저장
    current_state['last_user_input'] = action_text
    current_state['last_user_choice_idx'] = -1

    # 2. 게임 시작 여부 판단
    is_game_start = (
            action_text.lower() in ['시작', 'start', '게임시작'] and
            current_state.get('system_message') in ['Loaded', 'Init']
    )

    def generate():
        nonlocal session_id

        try:
            processed_state = current_state

            # [FIX] scenario_id로 시나리오 조회
            scenario_id = current_state.get('scenario_id')
            if not scenario_id:
                yield f"data: {json.dumps({'type': 'error', 'content': '시나리오 ID가 없습니다.'})}\n\n"
                return

            scenario = get_scenario_by_id(scenario_id)
            if not scenario:
                yield f"data: {json.dumps({'type': 'error', 'content': '시나리오를 찾을 수 없습니다.'})}\n\n"
                return

            # [FIX] WorldState 싱글톤 인스턴스 사용 - 변수명 wsm으로 통일
            wsm = WorldStateManager()

            if is_game_start:
                # 게임 시작 시: 세션이 있으면 유지, 없으면 새로 생성
                if not session_id:
                    wsm.reset()
                    wsm.initialize_from_scenario(scenario)
                    logger.info(f"🎮 [GAME START] New game session created")
                else:
                    logger.info(f"🎮 [GAME START] Resuming existing session: {session_id}")

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

                # [FIX] 게임 시작 시에도 location을 start_scene_id로 설정
                wsm.location = start_scene_id
            else:
                # 일반 턴: LangGraph 실행
                logger.info(f"🎮 Action: {action_text}")
                current_state['is_game_start'] = False
                processed_state = game_state.game_graph.invoke(current_state)
                game_state.state = processed_state

            # [경량화] WorldState를 player_state에 임시 추가 (저장용)
            processed_state['world_state'] = wsm.to_dict()

            # 🛠️ WorldState DB 저장 (매 턴마다)
            user_id = user.id if user else None

            # ✅ [중요] 세션 ID 유지 - 클라이언트가 보낸 세션 ID로 계속 저장
            if not session_id:
                # 새 세션 생성
                session_id = save_game_session(db, processed_state, user_id, None)
                logger.info(f"✅ [NEW SESSION] Created: {session_id}")
            else:
                # ✅ 기존 세션 업데이트 (클라이언트가 보낸 session_id 사용)
                session_id = save_game_session(db, processed_state, user_id, session_id)
                logger.info(f"✅ [SESSION UPDATE] Updated existing session: {session_id}")

            # 결과 추출
            npc_say = processed_state.get('npc_output', '')
            sys_msg = processed_state.get('system_message', '')
            intent = processed_state.get('parsed_intent')
            is_ending = (intent == 'ending')

            # --- [스트리밍 응답 전송] ---

            # ✅ [중요] 세션 ID 전송 (프론트엔드에서 저장)
            if session_id:
                yield f"data: {json.dumps({'type': 'session_id', 'content': session_id})}\n\n"

            # A. 시스템 메시지
            if sys_msg and "Game Started" not in sys_msg:
                sys_html = f"<div class='text-xs text-indigo-400 mb-2 border-l-2 border-indigo-500 pl-2'>🚀 {sys_msg}</div>"
                yield f"data: {json.dumps({'type': 'prefix', 'content': sys_html})}\n\n"

            # B. NPC 대화 (NPC 이름 표시)
            if npc_say:
                # 현재 씬에서 NPC 이름 가져오기
                curr_scene_id = processed_state['current_scene_id']
                all_scenes = {s['scene_id']: s for s in scenario.get('scenes', [])}
                curr_scene = all_scenes.get(curr_scene_id)
                npc_names = curr_scene.get('npcs', []) if curr_scene else []
                npc_name = npc_names[0] if npc_names else "NPC"

                npc_html = f"""
                <div class='bg-gradient-to-r from-yellow-900/30 to-yellow-800/20 p-4 rounded-lg border-l-4 border-yellow-500 mb-4 shadow-lg'>
                    <div class='flex items-center gap-2 mb-2'>
                        <i data-lucide="message-circle" class="w-4 h-4 text-yellow-400"></i>
                        <span class='text-yellow-400 font-bold text-sm uppercase tracking-wide'>{npc_name}</span>
                    </div>
                    <div class='text-gray-200 leading-relaxed pl-6'>{npc_say}</div>
                </div>
                """
                yield f"data: {json.dumps({'type': 'prefix', 'content': npc_html})}\n\n"

            # C. 프롤로그 (게임 시작 시)
            if is_game_start:
                prologue_text = scenario.get('prologue') or scenario.get('prologue_text', '')

                if prologue_text and prologue_text.strip():
                    prologue_html = '<div class="mb-6 p-4 bg-indigo-900/20 rounded-xl border border-indigo-500/30"><div class="text-indigo-400 font-bold text-sm mb-3 uppercase tracking-wider">[ Prologue ]</div><div class="text-gray-200 leading-relaxed serif-font text-lg">'
                    yield f"data: {json.dumps({'type': 'prefix', 'content': prologue_html})}\n\n"

                    for chunk in prologue_stream_generator(processed_state):
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
            stats_data = processed_state.get('player_vars', {})
            yield f"data: {json.dumps({'type': 'stats', 'content': stats_data})}\n\n"

            # [경량화] World State는 싱글톤 인스턴스에서 직접 가져옴 (디버그 모드용)
            world_state_data = wsm.to_dict()
            if world_state_data:
                # World State에 씬 정보 추가
                world_state_with_scene = world_state_data.copy()

                # [FIX] 현재 위치는 player_state의 current_scene_id를 우선적으로 사용 (더 정확함)
                location_scene_id = processed_state.get('current_scene_id') or world_state_with_scene.get('location', '')

                # 디버그 로그
                logger.info(
                    f"🗺️ [WORLD STATE] current_scene_id: {processed_state.get('current_scene_id')}, world_state location: {world_state_with_scene.get('location')}, using: {location_scene_id}")

                location_scene_title = ''

                # 시나리오에서 해당 씬의 title 또는 name 찾기
                if location_scene_id:
                    for scene in scenario.get('scenes', []):
                        if scene.get('scene_id') == location_scene_id:
                            # title 필드가 있으면 사용, 없으면 name 필드 사용
                            location_scene_title = scene.get('title') or scene.get('name', '')
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
                        'is_hostile': npc.get('isEnemy', False)
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
                            'is_hostile': npc_state.get('is_hostile', False)
                        }

            # 현재 씬의 NPC 위치 정보 업데이트
            all_scenes = {s['scene_id']: s for s in scenario.get('scenes', [])}
            for scene_id, scene in all_scenes.items():
                scene_title = scene.get('title', scene_id)
                for npc_name in scene.get('npcs', []) + scene.get('enemies', []):
                    if npc_name in all_scenario_npcs and all_scenario_npcs[npc_name]['location'] == '알 수 없음':
                        all_scenario_npcs[npc_name]['location'] = scene_title

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

        for chunk in scene_stream_generator(state, retry_count=retry_count, max_retries=MAX_RETRIES):
            # 재시도 신호 감지
            if "__RETRY_SIGNAL__" in chunk:
                need_retry = True
                break

            buffer += chunk
            yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"

        if need_retry:
            retry_count += 1
            if retry_count <= MAX_RETRIES:
                logger.info(f"🔄 [RETRY] Attempt {retry_count}/{MAX_RETRIES}")
                yield f"data: {json.dumps({'type': 'retry', 'attempt': retry_count, 'max': MAX_RETRIES})}\n\n"
            else:
                logger.warning(f"⚠️ [FALLBACK] Max retries exceeded")
                fallback_msg = get_narrative_fallback_message(state.get('scenario', {}))
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
        scenario = get_scenario_by_id(game_session.scenario_id)

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
                        'is_hostile': npc.get('isEnemy', False)
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
                        'is_hostile': npc_state.get('is_hostile', False)
                    }

        # 현재 씬의 NPC 위치 정보 업데이트
        if scenario:
            all_scenes = {s['scene_id']: s for s in scenario.get('scenes', [])}
            for scene_id, scene in all_scenes.items():
                scene_title = scene.get('title', scene_id)
                for npc_name in scene.get('npcs', []) + scene.get('enemies', []):
                    if npc_name in all_scenario_npcs and all_scenario_npcs[npc_name]['location'] == '알 수 없음':
                        all_scenario_npcs[npc_name]['location'] = scene_title

        # World State에 씬 정보 추가
        world_state_with_scene = game_session.world_state.copy() if game_session.world_state else {}

        # 현재 위치 scene_id 확인
        location_scene_id = world_state_with_scene.get('location') or game_session.current_scene_id
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
