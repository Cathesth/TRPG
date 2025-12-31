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


def _generate_single_scene(node_id: str, info: Dict, setting_data: Dict, skeleton: Dict, api_key: str, model_name: str = None) -> Dict:
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

        # [수정] 엔딩과 일반 씬의 프롬프트 및 출력 포맷 분리
        if is_ending:
            output_format = """
            {
                "title": "Creative Ending Title (Korean)",
                "description": "Rich ending description in Korean...",
                "condition": "The cause of this ending based on 'Came From' context (e.g., '전투 패배', '비밀 발견', '탈출 성공', '시간 초과') - Korean"
            }
            """
            game_mechanics_prompt = ""  # 엔딩은 전이(Transition)가 없으므로 메카닉 불필요
        else:
            output_format = """
            {
                "title": "Creative Title in Korean",
                "description": "Rich scene description in Korean...",
                "transitions": [
                    {
                        "trigger": "Action description in Korean",
                        "conditions": [
                            { "type": "stat_check", "stat": "STR", "value": 10 }
                        ],
                        "effects": []
                    }
                ]
            }
            """
            game_mechanics_prompt = """
            [GAME MECHANICS]
            - Add conditions (Stat/Item check) to transitions.
            - Add effects (Get Item, Change Stat) to transitions.
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
        - **Came From**: "{source_context}" (IMPORTANT: Reflect this context in the description/condition)

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

    # 3. [LLM Base] 논리적 흐름 검사 (룰 베이스 통과 시에만)
    prompt = f"""
    [TASK] Validate TRPG Scenario Logic.

    Data:
    Title: {scenario_data.get('title')}
    Scene Count: {len(scenes)}

    [CHECK]
    1. Is the story consistent?
    2. Are there any dead ends in normal scenes?

    [OUTPUT JSON]
    {{ "is_valid": true, "critical_issues": "None" }}
    """
    try:
        res = llm.invoke(prompt).content
        parsed = parse_json_garbage(res)
        return parsed.get('is_valid', True), parsed.get('critical_issues', 'None')
    except:
        return True, "None"


def _refine_scenario(scenario_data: Dict, issues: str, llm) -> Dict:
    """
    [Refiner Agent] 아주 강력한 수정 명령
    """
    logger.info(f"🛠️ [Refiner] Fixing Issues: {issues}")

    prompt = f"""
    [ROLE]
    You are a professional Korean TRPG Editor.

    [TASK]
    Fix the provided Scenario JSON based on issues: "{issues}".

    [CRITICAL INSTRUCTIONS]
    1. **TRANSLATE ALL ENGLISH TO KOREAN.** (Titles, Descriptions, Triggers, Conditions)
    2. **FILL EMPTY FIELDS.** If 'background_story' or 'prologue' is empty, write a creative one fitting the genre.
    3. **REPLACE 'Untitled'.** Create immersive titles for scenes.
    4. **FIX CONDITIONS.** Ensure ending conditions describe the cause (e.g., '전투 패배', '탈출 성공').
    5. **KEEP STRUCTURE.** Do NOT remove scenes or change IDs.

    [INPUT JSON]
    {json.dumps(scenario_data, ensure_ascii=False)}

    [OUTPUT]
    Return ONLY the corrected JSON.
    """

    try:
        # Deepseek/OpenAI 모델은 긴 컨텍스트 처리가 가능하므로 전체 전송
        res = llm.invoke(prompt).content
        fixed_data = parse_json_garbage(res)

        # 구조 체크
        if isinstance(fixed_data, dict) and ('scenes' in fixed_data or 'endings' in fixed_data):
            # 만약 리파이너가 실수로 scenes를 날렸으면 원본 복구 시도
            if 'scenes' not in fixed_data: fixed_data['scenes'] = scenario_data.get('scenes', [])
            if 'endings' not in fixed_data: fixed_data['endings'] = scenario_data.get('endings', [])

            logger.info("✅ [Refiner] Fixed successfully.")
            return fixed_data
        else:
            logger.warning("❌ [Refiner] Invalid structure. Using original.")
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


def generate_scenario_from_graph(api_key: str, react_flow_data: Dict[str, Any], model_name: str = None) -> Dict[str, Any]:
    logger.info("🚀 [Builder] Starting generation...")

    # 모델 선택: 전달된 model_name 사용, 없으면 DEFAULT_MODEL
    use_model = model_name if model_name else DEFAULT_MODEL
    logger.info(f"📦 Using model: {use_model}")

    try:
        parsed = parse_react_flow(react_flow_data)
        skeleton = parsed['skeleton']
        start_node_data = parsed.get('start_node_data')

        if not skeleton: return {"title": "Empty", "scenes": [], "endings": []}

        # 1. 사용자의 의도(장르, 설정 등) 추출
        user_prompt = ""
        if start_node_data:
            # Start 노드의 제목과 설명을 합쳐서 유저의 요구사항으로 간주
            user_prompt = f"Title: {start_node_data.get('title', '')}\nDescription: {start_node_data.get('description', '')}"

        # 만약 내용이 없으면 기본값
        if not user_prompt.strip() or user_prompt.strip() == "Title:\nDescription:":
            user_prompt = "Genre: General Fantasy"

        llm = LLMFactory.get_llm(api_key=api_key, model_name=use_model)
        titles = [s['title'] for s in skeleton.values()]

        # 2. 프롬프트에 user_prompt 추가 (강력하게 반영하도록 지시)
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
        except:
            setting_data = {"title": "New Adventure", "genre": "Adventure", "variables": []}

        final_scenes = []
        final_endings = []

        logger.info(f"Generating {len(skeleton)} scenes...")
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_node = {
                executor.submit(_generate_single_scene, nid, info, setting_data, skeleton, api_key, use_model): nid
                for nid, info in skeleton.items()
            }
            for future in as_completed(future_to_node):
                try:
                    res = future.result()
                    if res['type'] == 'ending':
                        final_endings.append(res['data'])
                    else:
                        final_scenes.append(res['data'])
                except:
                    pass

        # 프롤로그에서 연결된 첫 번째 씬 ID 저장
        first_scene_ids = []
        if start_node_data:
            first_scene_ids = start_node_data.get('connected_to', [])

        draft_scenario = {
            "title": setting_data.get('title', 'Untitled'),
            "genre": setting_data.get('genre', 'Adventure'),
            "background_story": setting_data.get('background_story', ''),
            "prologue": setting_data.get('prologue', ''),
            "prologue_connects_to": first_scene_ids,  # 프롤로그가 연결하는 씬 ID 목록
            "variables": setting_data.get('variables', []),
            "items": [],
            "npcs": [],
            "scenes": final_scenes,
            "endings": final_endings
        }

        # 검수 및 수정
        is_valid, issues = _validate_scenario(draft_scenario, llm)

        if not is_valid:
            final_result = _refine_scenario(draft_scenario, issues, llm)
            # prologue_connects_to 유지
            final_result['prologue_connects_to'] = first_scene_ids
            # ID 정규화 적용
            final_result = normalize_ids(final_result)
            logger.info("🎉 Generation Complete (Refined).")
            return final_result
        else:
            # ID 정규화 적용
            normalized_scenario = normalize_ids(draft_scenario)
            logger.info("🎉 Generation Complete (Direct Pass).")
            return normalized_scenario

    except Exception as e:
        logger.error(f"Critical Builder Error: {e}", exc_info=True)
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

