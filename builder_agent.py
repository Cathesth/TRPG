import os
import json
import logging
import sys
import re
from typing import Dict, Any, List, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from llm_factory import LLMFactory
from dotenv import load_dotenv

load_dotenv()

# --- [로깅 설정] ---
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('[%(asctime)s] %(levelname)s in %(module)s: %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

DEFAULT_MODEL = "openai/tngtech/deepseek-r1t2-chimera:free"

# --- [진행률 콜백 함수] ---
_progress_callback = None


def set_progress_callback(callback):
    """진행률 업데이트 콜백 설정"""
    global _progress_callback
    _progress_callback = callback


def _update_progress(status=None, step=None, detail=None, progress=None,
                     total_scenes=None, completed_scenes=None, current_phase=None):
    """진행률 업데이트 (콜백이 설정된 경우에만 호출)"""
    if _progress_callback:
        _progress_callback(
            status=status, step=step, detail=detail, progress=progress,
            total_scenes=total_scenes, completed_scenes=completed_scenes,
            current_phase=current_phase
        )


def parse_react_flow(react_flow_data: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("Parsing React Flow data...")
    nodes = react_flow_data.get('nodes', [])

    # connections도 edges로 처리
    edges = react_flow_data.get('edges', [])
    if not edges:
        edges = react_flow_data.get('connections', [])

    scenes_skeleton = {}
    adjacency_list = {}
    reverse_adjacency = {}

    for edge in edges:
        src = edge.get('source')
        tgt = edge.get('target')
        if src and tgt:
            if src not in adjacency_list: adjacency_list[src] = []
            if tgt not in adjacency_list[src]: adjacency_list[src].append(tgt)
            if tgt not in reverse_adjacency: reverse_adjacency[tgt] = []
            if src not in reverse_adjacency[tgt]: reverse_adjacency[tgt].append(src)

    start_node_id = None
    start_node_data = None

    for node in nodes:
        node_id = node.get('id')
        data = node.get('data', {})
        label = data.get('label', data.get('title', 'Untitled'))
        node_type = node.get('type', 'default')

        # start 노드 감지 (프롤로그/설정)
        is_start = (node_id == 'start' or node_type == 'start' or 'start' in label.lower() or node_type == 'input')

        if is_start:
            start_node_id = node_id
            start_node_data = {
                "node_id": node_id,
                "title": label,
                "description": data.get('description', ''),
                "connected_to": adjacency_list.get(node_id, [])
            }
            continue  # start 노드는 scenes_skeleton에 추가하지 않음

        targets = adjacency_list.get(node_id, [])
        sources = reverse_adjacency.get(node_id, [])

        scenes_skeleton[node_id] = {
            "scene_id": node_id,
            "title": label,
            "type": node_type,
            "connected_to": targets,
            "connected_from": sources
        }

    # start 노드의 연결 대상을 첫 번째 씬의 connected_from에 반영
    if start_node_data:
        for target_id in start_node_data.get('connected_to', []):
            if target_id in scenes_skeleton:
                if 'connected_from' not in scenes_skeleton[target_id]:
                    scenes_skeleton[target_id]['connected_from'] = []
                scenes_skeleton[target_id]['connected_from'].append('PROLOGUE')

    logger.info(f"Parsed {len(nodes)} nodes. Start Node: {start_node_id}, Scenes: {len(scenes_skeleton)}")
    return {
        "skeleton": scenes_skeleton,
        "start_node_id": start_node_id,
        "start_node_data": start_node_data,
        "node_count": len(nodes)
    }


def _generate_single_scene(node_id: str, info: Dict, setting_data: Dict, skeleton: Dict, api_key: str,
                           model_name: str = None) -> Dict:
    try:
        # 모델 선택
        use_model = model_name if model_name else DEFAULT_MODEL

        targets = info['connected_to']
        target_infos = []
        for idx, t_id in enumerate(targets):
            t_title = skeleton.get(t_id, {}).get('title', 'Unknown')
            target_infos.append(f"{idx + 1}. Destination: '{t_title}'")

        sources = info.get('connected_from', [])
        source_titles = [skeleton.get(s_id, {}).get('title', 'Unknown') for s_id in sources]
        source_context = ", ".join(source_titles) if source_titles else "Prologue"

        is_ending = (len(targets) == 0)

        scenario_title = setting_data.get('title', 'Unknown')
        genre = setting_data.get('genre', 'General')
        bg_story = setting_data.get('background_story', 'None')

        # ===== [Narrative Continuity 규칙] =====
        narrative_continuity_rules = """
        [NARRATIVE CONTINUITY - 인과관계 체인 규칙]
        이 규칙을 반드시 엄격히 준수하라:

        1. **인과관계 확인 (Causal Link)**
           - 이 씬의 시작은 이전 씬(Came From)에서 플레이어가 선택한 '트리거(Trigger)' 행동이 완료된 직후의 상황이어야 한다.
           - 예: 이전 씬에서 "문을 부수고 들어간다"를 선택했다면, 이 씬의 첫 문장은 문이 부서진 잔해나 그 소동으로 인한 주변의 반응으로 시작해야 함.
           - **첫 문단에 반드시 '이전 선택이 초래한 결과'를 배치하라.**

        2. **상태 및 환경의 전이 (Context Carry-over)**
           - 이전 씬에서 발생한 물리적 변화(불이 남, 물건이 파괴됨, NPC가 부상당함 등)는 이 씬의 배경 묘사에 지속적으로 포함되어야 한다.
           - 일회성 묘사가 아니라, 해당 사건이 현재 전개에 어떤 영향을 주는지 명시하라.

        3. **선택지의 무게감 (Weight of Choice)**
           - 선택지는 단순히 씬을 이동시키는 버튼이 아니다.
           - 각 선택지는 플레이어의 스탯 변화뿐만 아니라, **'서사적 태그'**를 남겨야 한다.
           - 다음 씬은 "플레이어가 [어떤 선택]을 통해 이 씬에 도달했음"을 인지하고 그에 맞는 톤앤매너를 유지해야 한다.

        4. **논리적 일관성 체크 (Consistency Check)**
           - 이전 씬에서 NPC가 죽었다면 이 씬에서 그 NPC가 다시 등장해서는 안 된다.
           - 모든 씬은 전체 세계관 설명(Background)과 이전 선택지의 결과라는 두 가지 축을 중심으로 논리적으로 구성되어야 한다.

        [핵심 지시]
        단순한 묘사가 아니라, '이전 선택이 초래한 결과'를 첫 문단에 배치하고, 그 결과가 현재 씬의 분위기를 어떻게 지배하고 있는지 서술하라.
        """

        # [수정] 엔딩과 일반 씬의 프롬프트 및 출력 포맷 분리
        if is_ending:
            output_format = """
            {
                "title": "Creative Ending Title (Korean)",
                "description": "Rich ending description in Korean. 첫 문단은 반드시 이전 씬에서의 선택 결과로 시작해야 함.",
                "condition": "The cause of this ending based on 'Came From' context (e.g., '전투 패배', '비밀 발견', '탈출 성공', '시간 초과') - Korean"
            }
            """
            game_mechanics_prompt = ""  # 엔딩은 전이(Transition)가 없으므로 메카닉 불필요
        else:
            output_format = """
            {
                "title": "Creative Title in Korean",
                "description": "Rich scene description in Korean. 첫 문단은 반드시 이전 씬에서의 선택 결과로 시작해야 함. 이 결과가 현재 씬의 분위기를 어떻게 지배하는지 서술.",
                "transitions": [
                    {
                        "trigger": "Simple Action description in Korean (예: '문을 연다', '망치로 벽을 부순다')",
                        "conditions": [
                            { "type": "stat_check", "stat": "STR", "value": 10 }
                        ],
                        "effects": [
                            { "type": "change_stat", "stat": "HP", "value": -10 }
                        ],
                        "narrative_tag": "이 선택의 서사적 의미 (예: '폭력적 해결', '은밀한 접근', '희생적 선택')"
                    }
                ]
            }
            """
            # [수정] 게임 메카닉 및 행동 정의 규칙 강화
            game_mechanics_prompt = """
            [GAME MECHANICS & ACTION RULES]
            1. **Simple Triggers (Actions)**:
               - Actions MUST be simple and direct. Format: "Object + Verb".
               - Example: "문을 연다" (Open door), "열쇠를 줍는다" (Pick up key), "도망친다" (Run away).
               - **DO NOT** use complex sentences like "열쇠를 주워서 문을 열고 나간다". Break it down.

            2. **Key Item Actions**:
               - If an action requires a specific item, explicitly state it in the trigger.
               - Format: "Item + Target + Verb".
               - Example: "망치로 벽을 부순다" (Break wall with hammer), "열쇠로 상자를 연다" (Open box with key).

            3. **Item Acquisition & Hints**:
               - If this scene involves obtaining a key item (effect: gain_item), the 'description' MUST provide a **subtle hint** about its future use.
               - Example: When getting a 'Hammer', describe: "A heavy hammer lies there. It looks strong enough to break a cracked wall."
               - The trigger to get the item should simply be "망치를 줍는다" (Pick up hammer).

            4. **Logical Conditions**:
               - Add logical 'conditions' for transitions (e.g., must have 'Key' to 'Open locked door').
            """

        prompt = f"""
        [TASK]
        Write a TRPG scene content for "{scenario_title}".

        [LANGUAGE]
        **KOREAN ONLY.** (Must write Title and Description in Korean).

        [WORLD SETTING]
        - Genre: {genre}
        - Background: {bg_story}

        [SCENE INFO]
        - Current Title: "{info['title']}"
        - Type: {"Ending Scene" if is_ending else "Normal Scene"}
        - **Came From**: "{source_context}" (CRITICAL: 이 씬의 첫 문단은 이전 씬에서의 선택 결과를 반영해야 함)

        {narrative_continuity_rules}

        [REQUIRED TRANSITIONS]
        Destinations:
        {chr(10).join(target_infos) if targets else "None (Ending)"}

        {game_mechanics_prompt}

        [OUTPUT JSON FORMAT]
        {output_format}
        """

        llm = LLMFactory.get_llm(api_key=api_key, model_name=use_model)
        response = llm.invoke(prompt).content
        scene_data = parse_json_garbage(response)

        title = scene_data.get('title', info['title'])
        description = scene_data.get('description', '내용 없음')

        result = {"type": "ending" if is_ending else "scene", "data": None}

        if is_ending:
            # [수정] AI가 생성한 condition 사용, 없으면 문맥 기반 기본값
            condition_text = scene_data.get('condition')
            if not condition_text:
                condition_text = f"{source_context}에서의 결과"

            result['data'] = {
                "ending_id": node_id,
                "title": title,
                "description": description,
                "condition": condition_text
            }
        else:
            mapped_transitions = []
            generated_transitions = scene_data.get('transitions', [])

            for i, real_target_id in enumerate(targets):
                target_title = skeleton[real_target_id]['title']

                # 기본값
                trigger_text = f"이동"
                conditions = []
                effects = []

                if i < len(generated_transitions):
                    gen_trans = generated_transitions[i]
                    trigger_text = gen_trans.get('trigger', trigger_text)
                    conditions = gen_trans.get('conditions', [])
                    effects = gen_trans.get('effects', [])

                mapped_transitions.append({
                    "target_scene_id": real_target_id,
                    "trigger": trigger_text,
                    "conditions": conditions,
                    "effects": effects
                })

            result['data'] = {
                "scene_id": node_id,
                "title": title,
                "description": description,
                "image_prompt": f"{genre} style scene: {title}, {description[:30]}",
                "transitions": mapped_transitions,
                "npcs": []
            }
        return result

    except Exception as e:
        logger.error(f"Scene Gen Error ({node_id}): {e}")
        targets = info.get('connected_to', [])
        fallback_data = {
            "scene_id": node_id,
            "title": info.get('title', 'Error'),
            "description": "생성 중 오류가 발생했습니다.",
            "transitions": [{"target_scene_id": t, "trigger": "이동"} for t in targets],
            "npcs": []
        }
        return {"type": "scene", "data": fallback_data}


def _validate_scenario(scenario_data: Dict, llm) -> Tuple[bool, str]:
    """
    [Validator Agent] 룰 베이스 + LLM 하이브리드 검수
    - 인과관계 체인(Narrative Continuity) 검증 포함
    """
    logger.info("🔍 [Validator] Checking scenario...")

    issues = []

    # 1. [Rule Base] 필수 필드 누락 검사
    if not scenario_data.get('background_story') or len(scenario_data.get('background_story')) < 10:
        issues.append("Missing or too short 'background_story'.")

    if not scenario_data.get('prologue'):
        issues.append("Missing 'prologue'.")

    scenes = scenario_data.get('scenes', [])
    endings = scenario_data.get('endings', [])

    # 2. [Rule Base] 'Untitled' 및 영어 텍스트 감지
    english_pattern = re.compile(r'[a-zA-Z]{5,}')  # 영단어 5글자 이상 연속되면 의심

    for s in scenes + endings:
        t_title = s.get('title', '')
        t_desc = s.get('description', '')

        if 'Untitled' in t_title or '내용 없음' in t_desc:
            issues.append(f"Scene '{s.get('scene_id', s.get('ending_id'))}' has placeholder content (Untitled/Empty).")
            break

        # 영어 감지 (설명에 영어가 너무 많으면)
        if len(re.findall(english_pattern, t_desc)) > 3:
            issues.append("Content detected in English. Must be Korean.")
            break

    if not scenes and not endings:
        return False, "Scenario is completely empty."

    # 이슈가 발견되면 즉시 리턴 (LLM 아낌)
    if issues:
        return False, ", ".join(issues)

    # 3. [LLM Base] 논리적 흐름 + 인과관계 체인 검사 (룰 베이스 통과 시에만)
    prompt = f"""
    [TASK] Validate TRPG Scenario Logic and Narrative Continuity.

    Data:
    Title: {scenario_data.get('title')}
    Scene Count: {len(scenes)}
    Ending Count: {len(endings)}

    [CHECK - 인과관계 체인 규칙]
    1. **Causal Link**: 각 씬의 시작이 이전 씬의 선택 결과를 반영하는가?
    2. **Context Carry-over**: 이전 씬에서 발생한 물리적 변화가 다음 씬에 지속되는가?
    3. **Consistency Check**: 죽은 NPC가 다시 등장하거나, 논리적 모순이 있는가?
    4. **Dead Ends**: 일반 씬에서 막다른 길(연결 없음)이 있는가?
    5. **Story Flow**: 전체적인 서사 흐름이 일관성 있는가?

    [OUTPUT JSON]
    {{ "is_valid": true, "critical_issues": "None" }}

    If issues found:
    {{ "is_valid": false, "critical_issues": "씬 간 인과관계 부족, NPC 일관성 오류 등 구체적 문제점" }}
    """
    try:
        res = llm.invoke(prompt).content
        parsed = parse_json_garbage(res)
        return parsed.get('is_valid', True), parsed.get('critical_issues', 'None')
    except:
        return True, "None"


def _refine_scenario(scenario_data: Dict, issues: str, llm) -> Dict:
    """
    [Refiner Agent - Patch Mode]
    전체 시나리오를 다시 생성하지 않고, 수정이 필요한 부분만 JSON으로 반환받아 병합합니다.
    """
    logger.info(f"🛠️ [Refiner] Patching Issues: {issues}")

    prompt = f"""
    [ROLE]
    You are a professional Korean TRPG Editor.

    [TASK]
    Fix the provided Scenario based on issues: "{issues}".

    [CONSTRAINT] 
    1. **DO NOT REWRITE THE WHOLE SCENARIO.** (It is too long)
    2. Return a JSON containing **ONLY the fields or scenes that need changes.**
    3. If a specific scene needs fixing, include its 'scene_id' and the updated fields (title, description, transitions).
    4. If prologue needs fixing, include 'prologue'.
    5. **ALL CONTENT MUST BE IN KOREAN.**

    [INPUT CONTEXT]
    Title: {scenario_data.get('title')}
    Background: {scenario_data.get('background_story')[:200]}...
    Current Scene IDs: {[s['scene_id'] for s in scenario_data.get('scenes', [])]}

    [OUTPUT JSON FORMAT]
    {{
        "prologue": "Updated prologue text (optional, only if needed)",
        "scenes_to_update": [
            {{ 
                "scene_id": "target_scene_id", 
                "title": "Fixed Title (Korean)", 
                "description": "Fixed Description (Korean)" 
            }}
        ]
    }}
    """

    try:
        res = llm.invoke(prompt).content
        patch_data = parse_json_garbage(res)

        # 1. 프롤로그 수정 적용
        if 'prologue' in patch_data and patch_data['prologue']:
            scenario_data['prologue'] = patch_data['prologue']
            logger.info("✅ [Refiner] Prologue patched.")

        # 2. 씬 수정 적용
        updates = {s['scene_id']: s for s in patch_data.get('scenes_to_update', []) if 'scene_id' in s}

        if updates:
            updated_count = 0
            # 기존 씬 리스트를 순회하며 ID가 일치하면 업데이트
            for scene in scenario_data.get('scenes', []):
                sid = scene.get('scene_id')
                if sid in updates:
                    logger.info(f"✅ [Refiner] Patching scene {sid}...")
                    # 기존 데이터에 업데이트 데이터 병합 (덮어쓰기)
                    scene.update(updates[sid])
                    updated_count += 1

            # 엔딩도 체크 (혹시 엔딩을 수정했을 경우)
            for ending in scenario_data.get('endings', []):
                eid = ending.get('ending_id')
                if eid in updates:
                    logger.info(f"✅ [Refiner] Patching ending {eid}...")
                    ending.update(updates[eid])
                    updated_count += 1

            logger.info(f"✅ [Refiner] Total {updated_count} scenes/endings updated.")

        return scenario_data

    except Exception as e:
        logger.error(f"Refiner Error: {e}")
        return scenario_data


def normalize_ids(scenario_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    씬과 엔딩의 ID를 간단한 형식으로 정규화합니다.
    scene-1766998232980 -> scene-1, scene-2, ...
    ending-1766998240477 -> ending-1, ending-2, ...
    모든 연결 정보(transitions, prologue_connects_to)도 함께 업데이트합니다.
    """
    id_map = {}  # { old_id: new_id }

    scenes = scenario_data.get('scenes', [])
    endings = scenario_data.get('endings', [])

    # 1. 씬 ID 매핑 생성 (scene-1, scene-2, ...)
    for idx, scene in enumerate(scenes, start=1):
        old_id = scene.get('scene_id')
        new_id = f"scene-{idx}"
        if old_id:
            id_map[old_id] = new_id
            scene['scene_id'] = new_id

    # 2. 엔딩 ID 매핑 생성 (ending-1, ending-2, ...)
    for idx, ending in enumerate(endings, start=1):
        old_id = ending.get('ending_id')
        new_id = f"ending-{idx}"
        if old_id:
            id_map[old_id] = new_id
            ending['ending_id'] = new_id

    # 3. Transitions(연결) 정보 업데이트
    for scene in scenes:
        for trans in scene.get('transitions', []):
            target = trans.get('target_scene_id')
            if target and target in id_map:
                trans['target_scene_id'] = id_map[target]

    # 4. 프롤로그 연결 정보 업데이트 (매핑된 ID만 포함)
    prologue_connects_to = scenario_data.get('prologue_connects_to', [])
    new_prologue_connects = [id_map[old_id] for old_id in prologue_connects_to if old_id in id_map]
    scenario_data['prologue_connects_to'] = new_prologue_connects

    logger.info(f"✅ [normalize_ids] ID 정규화 완료: {len(id_map)} IDs mapped")

    return scenario_data


def generate_scenario_from_graph(api_key: str, react_flow_data: Dict[str, Any], model_name: str = None) -> Dict[
    str, Any]:
    logger.info("🚀 [Builder] Starting generation...")

    # 모델 선택: 전달된 model_name 사용, 없으면 DEFAULT_MODEL
    use_model = model_name if model_name else DEFAULT_MODEL
    logger.info(f"📦 Using model: {use_model}")

    try:
        # Phase 1: 그래프 파싱
        _update_progress(
            status="building",
            current_phase="parsing",
            step="1/5",
            detail="노드 그래프 분석 중...",
            progress=5
        )

        parsed = parse_react_flow(react_flow_data)
        skeleton = parsed['skeleton']
        start_node_data = parsed.get('start_node_data')
        total_scene_count = len(skeleton)

        _update_progress(
            detail=f"총 {total_scene_count}개의 씬 감지됨",
            progress=10,
            total_scenes=total_scene_count,
            completed_scenes=0
        )

        if not skeleton:
            _update_progress(status="error", detail="씬이 없습니다")
            return {"title": "Empty", "scenes": [], "endings": []}

        # 1. 사용자의 의도(장르, 설정 등) 추출
        user_prompt = ""
        if start_node_data:
            user_prompt = f"Title: {start_node_data.get('title', '')}\nDescription: {start_node_data.get('description', '')}"

        if not user_prompt.strip() or user_prompt.strip() == "Title:\nDescription:":
            user_prompt = "Genre: General Fantasy"

        # Phase 2: 세계관 생성
        _update_progress(
            current_phase="worldbuilding",
            step="2/5",
            detail="세계관 및 프롤로그 생성 중...",
            progress=15
        )

        llm = LLMFactory.get_llm(api_key=api_key, model_name=use_model)
        titles = [s['title'] for s in skeleton.values()]

        setting_prompt = f"""
            [TASK] Create a TRPG world setting.

            [USER REQUEST - MUST FOLLOW]
            {user_prompt}

            [SCENE TITLES FOR REFERENCE]
            {', '.join(titles)}

            [RULES]
            1. The genre and background_story MUST match what the user requested above.
            2. Do NOT ignore or change the user's specified genre/theme.
            3. All text must be in Korean.

            [OUTPUT JSON]
            {{
                "title": "창의적인 시나리오 제목",
                "genre": "사용자가 요청한 장르",
                "background_story": "세계관 설명 (3문장 이상)",
                "prologue": "프롤로그 장면 묘사",
                "variables": [
                    {{ "name": "HP", "initial_value": 100 }},
                    {{ "name": "SANITY", "initial_value": 100 }}
                ]
            }}
            """
        try:
            setting_res = llm.invoke(setting_prompt).content
            setting_data = parse_json_garbage(setting_res)
            _update_progress(
                detail=f"세계관 '{setting_data.get('title', '?')}' 생성 완료",
                progress=25
            )
        except:
            setting_data = {"title": "New Adventure", "genre": "Adventure", "variables": []}

        # Phase 3: 씬 생성 (병렬 처리)
        _update_progress(
            current_phase="scene_generation",
            step="3/5",
            detail=f"씬 콘텐츠 생성 시작 (0/{total_scene_count})",
            progress=30
        )

        final_scenes = []
        final_endings = []
        completed_count = 0

        logger.info(f"Generating {len(skeleton)} scenes...")
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_node = {
                executor.submit(_generate_single_scene, nid, info, setting_data, skeleton, api_key, use_model): nid
                for nid, info in skeleton.items()
            }
            for future in as_completed(future_to_node):
                node_id = future_to_node[future]
                try:
                    res = future.result()
                    completed_count += 1

                    # 씬 생성 진행률 업데이트
                    scene_progress = 30 + int((completed_count / total_scene_count) * 45)
                    scene_title = res.get('data', {}).get('title', node_id)
                    _update_progress(
                        detail=f"씬 생성 완료: '{scene_title}' ({completed_count}/{total_scene_count})",
                        progress=scene_progress,
                        completed_scenes=completed_count
                    )

                    if res['type'] == 'ending':
                        final_endings.append(res['data'])
                    else:
                        final_scenes.append(res['data'])
                except Exception as e:
                    completed_count += 1
                    logger.error(f"Scene generation failed for {node_id}: {e}")

        # 프롤로그에서 연결된 첫 번째 씬 ID 저장
        first_scene_ids = []
        if start_node_data:
            first_scene_ids = start_node_data.get('connected_to', [])

        draft_scenario = {
            "title": setting_data.get('title', 'Untitled'),
            "genre": setting_data.get('genre', 'Adventure'),
            "background_story": setting_data.get('background_story', ''),
            "prologue": setting_data.get('prologue', ''),
            "prologue_connects_to": first_scene_ids,
            "variables": setting_data.get('variables', []),
            "items": [],
            "npcs": [],
            "scenes": final_scenes,
            "endings": final_endings
        }

        # Phase 4: 검증
        _update_progress(
            current_phase="validation",
            step="4/5",
            detail="시나리오 일관성 검증 중...",
            progress=80
        )

        is_valid, issues = _validate_scenario(draft_scenario, llm)

        if not is_valid:
            # Phase 5: 수정 (필요 시)
            _update_progress(
                current_phase="refining",
                step="5/5",
                detail=f"품질 개선 중: {issues[:50]}...",
                progress=85
            )

            # Refine 단계에서 Patch 방식으로 수정
            final_result = _refine_scenario(draft_scenario, issues, llm)
            final_result['prologue_connects_to'] = first_scene_ids

            _update_progress(detail="ID 정규화 중...", progress=92)
            final_result = normalize_ids(final_result)

            _update_progress(
                status="completed",
                current_phase="done",
                step="완료",
                detail=f"시나리오 '{final_result.get('title')}' 생성 완료! (수정됨)",
                progress=100
            )

            logger.info("🎉 Generation Complete (Refined).")
            return final_result
        else:
            # Phase 5: 완료
            _update_progress(
                current_phase="finalizing",
                step="5/5",
                detail="ID 정규화 및 최종 처리 중...",
                progress=90
            )

            normalized_scenario = normalize_ids(draft_scenario)

            _update_progress(
                status="completed",
                current_phase="done",
                step="완료",
                detail=f"시나리오 '{normalized_scenario.get('title')}' 생성 완료!",
                progress=100
            )

            logger.info("🎉 Generation Complete (Direct Pass).")
            return normalized_scenario

    except Exception as e:
        logger.error(f"Critical Builder Error: {e}", exc_info=True)
        _update_progress(
            status="error",
            current_phase="error",
            step="오류",
            detail=f"생성 실패: {str(e)[:100]}",
            progress=0
        )
        return {"title": "Error", "scenes": [], "endings": []}


def parse_json_garbage(text: str) -> Dict[str, Any]:
    if isinstance(text, dict): return text
    if not text: return {}
    try:
        text = text.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]

        parsed = json.loads(text)
        if isinstance(parsed, str):
            try:
                parsed = json.loads(parsed)
            except:
                pass
        return parsed if isinstance(parsed, dict) else {}
    except:
        try:
            start = text.find('{')
            end = text.rfind('}') + 1
            if start != -1 and end != -1:
                return json.loads(text[start:end])
        except:
            pass
        return {}