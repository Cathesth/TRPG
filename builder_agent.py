import json
from typing import List
from crewai import Agent, Task, Crew
from llm_factory import get_builder_model
from schemas import GameScenario  # 업데이트된 스키마 사용


# --- Helper Functions ---

def transform_graph_to_draft(nodes: List[dict], edges: List[dict], global_npcs: List[dict]) -> dict:
    """
    React Flow의 그래프 데이터를 CrewAI가 이해할 수 있는 Draft JSON으로 변환.
    Choice가 아니라 'Transition Draft'를 생성함.
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
            transitions_draft = []  # choices_draft -> transitions_draft
            for edge in my_edges:
                transitions_draft.append({
                    "target_scene_id": edge['target'],
                    "trigger": "",  # AI가 채워야 할 '행동 조건' (예: 문을 연다, 설득한다)
                    "conditions": [],
                    "effects": []
                })

            scenes.append({
                "scene_id": node['id'],
                "title": data.get('title', ''),
                "description": data.get('description', ''),
                "npc_names": data.get('npcs', []),
                "transitions_draft": transitions_draft  # 필드명 변경
            })
        elif node_type == 'ending':
            endings.append({
                "ending_id": node['id'],
                "title": data.get('title', ''),
                "condition": data.get('condition', '')
            })

    return {
        "title": start_node.get('label', 'Untitled Scenario') if start_node else "Untitled",
        "genre": "Dark Fantasy",
        "background": start_node.get('description', '') if start_node else "",
        "global_npcs": global_npcs,
        "scenes": scenes,
        "endings": endings
    }


# --- Main Crew Logic ---

def generate_scenario_from_graph(api_key: str, react_flow_data: dict):
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
        goal='Create an immersive story and define LOGICAL TRANSITIONS between scenes.',
        backstory="""
        You are a legendary RPG Maker. 

        Your tasks:
        1. Write Pure Narrative Descriptions for scenes (NO choice lists inside description).
        2. Define `transitions` for each scene based on the graph structure.
           - Instead of writing a "Choice Button Text", describe the **User Action (Trigger)** that causes the transition.
           - Example Trigger: "Player attacks the guard", "Player successfully picks the lock", "Player gives the potion".

        You also define Global Variables and Items based on the story.
        """,
        llm=claude,
        verbose=True
    )

    # [Agent 2] 텍스트 & 로직 검수
    korean_editor = Agent(
        role='Korean Editor & Logic Polisher',
        goal='Ensure immersive tone and verify transition logic.',
        backstory="""
        You are a meticulous editor. 
        1. Ensure the Korean text is natural and immersive.
        2. CHECK TRANSITIONS: Ensure the `trigger` for each transition is a clear action description, NOT a UI button text like "1. Go next".
           - Bad: "1. Open Door"
           - Good: "Player opens the iron door"
        """,
        llm=claude,
        verbose=True
    )

    # [Agent 3] 스키마 집행자
    consistency_enforcer = Agent(
        role='Schema Consistency Enforcer',
        goal='Produce a strictly valid JSON object matching the GameScenario schema.',
        backstory="""
        You are a compiler. Your job is to structure the output into the `GameScenario` JSON format.

        CRITICAL:
        - Map the drafted transitions to `transitions` list in the schema.
        - Ensure `trigger` fields explain the action required to move to `target_scene_id`.
        - Ensure `variables` and `items` are consistent.
        """,
        llm=claude,
        verbose=True
    )

    # 4. 태스크 정의

    task_design = Task(
        description=f"""
        Analyze this draft: {json.dumps(draft_data, ensure_ascii=False)}

        1. **System**: Define GlobalVariables and Items.
        2. **Narrative**: Write pure story descriptions for scenes.
        3. **Transitions**:
           - For each `transitions_draft` entry, write a `trigger` string describing the action.
           - Add `effects` (e.g., lose HP) and `conditions` (e.g., has Key) if necessary.
        4. **Visuals**: Generate `image_prompt`.
        """,
        agent=game_designer,
        expected_output="Draft with story, variables, items, and action-based transitions."
    )

    task_polish = Task(
        description="""
        Review the draft.
        1. Polish Korean text.
        2. **Verify Triggers**: Ensure `trigger` describes an action, not a menu option.
        3. Check Logic: Ensure effects/conditions use valid variable/item names.
        """,
        agent=korean_editor,
        expected_output="Polished draft with logical transitions."
    )

    task_finalize = Task(
        description="""
        Finalize into `GameScenario` schema.
        Ensure `transitions` are correctly populated instead of choices.
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