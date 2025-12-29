import json
from typing import List
from crewai import Agent, Task, Crew
from llm_factory import get_builder_model
from schemas import GameScenario  # 업데이트된 스키마 사용


# --- Helper Functions ---

def transform_graph_to_draft(nodes: List[dict], edges: List[dict], global_npcs: List[dict]) -> dict:
    """
    React Flow의 그래프 데이터를 CrewAI가 이해할 수 있는 Draft JSON으로 변환.
    """
    scenes = []
    endings = []
    start_node = None

    for node in nodes:
        node_type = node['type']
        data = node['data']

        if node_type == 'start':
            start_node = data
        elif node_type == 'scene':
            my_edges = [e for e in edges if e['source'] == node['id']]
            choices_draft = []
            for edge in my_edges:
                choices_draft.append({
                    "text": "",
                    "next_scene_id": edge['target'],
                    "conditions": [],  # 로직 추가용 빈 슬롯
                    "effects": []
                })

            scenes.append({
                "scene_id": node['id'],
                "title": data.get('title', ''),
                "description": data.get('description', ''),
                # image_prompt는 AI가 생성하도록 비워둠
                "npc_names": data.get('npcs', []),
                "choices_draft": choices_draft
            })
        elif node_type == 'ending':
            endings.append({
                "ending_id": node['id'],
                "title": data.get('title', ''),
                "condition": data.get('condition', '')  # 텍스트 조건
            })

    return {
        "title": start_node.get('label', 'Untitled Scenario') if start_node else "Untitled",
        "genre": "Dark Fantasy",  # 기본값, 필요시 파라미터로 받음
        "background": start_node.get('description', '') if start_node else "",
        "global_npcs": global_npcs,
        "scenes": scenes,
        "endings": endings
    }


# --- Main Crew Logic ---

def generate_scenario_from_graph(api_key: str, react_flow_data: dict):
    """
    React Flow Data -> Draft JSON -> CrewAI (Gen -> Edit -> Fix) -> Final JSON
    """

    # 1. 데이터 변환
    nodes = react_flow_data.get('nodes', [])
    edges = react_flow_data.get('edges', [])
    global_npcs = react_flow_data.get('globalNpcs', [])

    draft_data = transform_graph_to_draft(nodes, edges, global_npcs)

    # 2. 모델 설정
    claude = get_builder_model(api_key)

    # 3. 에이전트 정의

    # [Agent 1] 게임 시스템 기획자 & 작가
    game_designer = Agent(
        role='Game Systems Designer & Writer',
        goal='Create an immersive story AND robust game mechanics (variables, items).',
        backstory="""
        You are a legendary RPG Maker. 
        Your main job is to write Pure Narrative Descriptions for scenes.

        CRITICAL RULE:
        - NEVER include choice lists (e.g., "1. Go left") or user prompts (e.g., "What do you do?") inside the scene `description`.
        - The `description` should ONLY contain what the character sees, hears, and feels. The choices are handled by the UI, not the text.

        You also define Global Variables (e.g., HP, Sanity, Gold) and Items based on the story.
        """,
        llm=claude,
        verbose=True
    )

    # [Agent 2] 텍스트 & 로직 검수
    korean_editor = Agent(
        role='Korean Editor & Logic Polisher',
        goal='Ensure immersive tone and CLEAN descriptions without choice texts.',
        backstory="""
        You are a meticulous editor. 
        1. Ensure the Korean text is natural and immersive (Dark Fantasy tone).
        2. CLEANUP: If the `description` contains text like "당신의 선택은?" or list of choices, DELETE IT immediately. Keep only the narrative.
        3. LOGIC: Check if the game logic makes sense (e.g., don't spend Gold if you never gave Gold).
        """,
        llm=claude,
        verbose=True
    )

    # [Agent 3] 스키마 집행자 (Consistency Enforcer)
    consistency_enforcer = Agent(
        role='Schema Consistency Enforcer',
        goal='Produce a strictly valid JSON object matching the GameScenario schema.',
        backstory="""
        You are a compiler. Your only job is to take the previous output and structure it 
        perfectly into the `GameScenario` JSON format.
        You must ensure:
        1. All referenced `items` in effects/conditions exist in the `items` list.
        2. All `variables` (HP, etc.) are initialized in `variables` list.
        3. `next_scene_id` links are valid.
        4. No hallucinations in NPC names.
        """,
        llm=claude,
        verbose=True
    )

    # 4. 태스크 정의

    # Task 1: 기획 및 초안 작성
    task_design = Task(
        description=f"""
        Analyze this draft: {json.dumps(draft_data, ensure_ascii=False)}

        1. **System Design**: Define 2-3 `GlobalVariables` and 3-5 `Items`.
        2. **Narrative**: Write descriptions for all scenes. 
           - **CONSTRAINT**: The `description` MUST be pure storytelling. DO NOT write "1. Attack 2. Run" inside the description.
        3. **Logic Implementation**: 
           - For choices, add `effects` and `conditions`.
        4. **Visuals**: Generate a short English `image_prompt` for scenes/NPCs.
        """,
        agent=game_designer,
        expected_output="Detailed draft with pure narrative descriptions (no choice text inside) and game logic."
    )

    # Task 2: 윤문 및 로직 체크
    task_polish = Task(
        description="""
        Review the draft.
        1. Polish Korean text to be serious and immersive.
        2. **Verify Descriptions**: Ensure NO scene description ends with "What will you do?" or choice lists. If found, remove that part.
        3. Check Logic: Ensure effects use correct variable names defined in step 1.
        """,
        agent=korean_editor,
        expected_output="Polished draft with clean descriptions and verified logic."
    )

    # Task 3: 최종 JSON 변환
    task_finalize = Task(
        description="""
        Finalize the output into `GameScenario` schema.

        CRITICAL CHECKS:
        - Ensure `variables` list contains all variables used in effects.
        - Ensure `items` list contains all items used in conditions/effects.
        - Ensure `next_scene_id` allows the game to be playable from start to end.
        """,
        agent=consistency_enforcer,
        expected_output="Final valid JSON object.",
        output_pydantic=GameScenario
    )

    # 5. 실행
    crew = Crew(
        agents=[game_designer, korean_editor, consistency_enforcer],
        tasks=[task_design, task_polish, task_finalize],
        verbose=True
    )

    result = crew.kickoff()

    return result.pydantic.dict()