import json
import time
from typing import List, Dict, Any, Optional
from crewai import Agent, Task, Crew
from llm_factory import get_builder_model
from schemas import GameScenario  # 분리된 스키마 임포트


# --- Helper Functions ---

def calculate_estimated_time(node_count: int, complexity: str = "medium") -> int:
    """
    Estimates generation time based on graph size.
    Rule of thumb: 10s per node + 20s overhead + validation time.
    """
    base_time = 20
    per_node = 10
    if complexity == "high":
        per_node = 15

    total_seconds = base_time + (node_count * per_node)
    return total_seconds


def transform_graph_to_draft(nodes: List[dict], edges: List[dict], global_npcs: List[dict]) -> dict:
    """
    React Flow의 그래프 데이터를 CrewAI가 이해할 수 있는 Draft JSON으로 변환.
    연결선(Edge)을 분석하여 next_scene_id 등을 매핑함.
    """
    scenes = []
    endings = []
    start_node = None

    # 1. 노드 분류
    for node in nodes:
        node_type = node['type']
        data = node['data']

        if node_type == 'start':
            start_node = data
        elif node_type == 'scene':
            # Edge를 찾아 연결된 다음 씬 확인
            my_edges = [e for e in edges if e['source'] == node['id']]
            choices_draft = []

            # 연결된 노드 개수만큼 선택지 초안 생성 (나중에 AI가 텍스트 채움)
            for idx, edge in enumerate(my_edges):
                target_id = edge['target']
                choices_draft.append({
                    "text": "",  # AI가 채워야 함
                    "next_scene_id": target_id
                })

            scenes.append({
                "scene_id": node['id'],
                "title": data.get('title', ''),
                "description": data.get('description', ''),  # 비어있으면 AI가 생성
                "required_item": data.get('keyItem', None),
                "required_action": data.get('action', None),
                "npc_names": data.get('npcs', []),
                "choices_draft": choices_draft
            })
        elif node_type == 'ending':
            endings.append({
                "ending_id": node['id'],
                "title": data.get('title', ''),
                "condition": data.get('condition', '')
            })

    return {
        "title": start_node.get('label', 'Untitled Scenario') if start_node else "Untitled",
        "background": start_node.get('description', '') if start_node else "",  # Start node description -> background
        "global_npcs": global_npcs,
        "scenes": scenes,
        "endings": endings
    }


# --- Main Crew Logic ---

def generate_scenario_from_graph(api_key: str, react_flow_data: dict):
    """
    React Flow Data -> Draft JSON -> CrewAI -> Final JSON
    """

    # 1. 데이터 변환
    nodes = react_flow_data.get('nodes', [])
    edges = react_flow_data.get('edges', [])
    global_npcs = react_flow_data.get('globalNpcs', [])

    draft_data = transform_graph_to_draft(nodes, edges, global_npcs)

    # 2. 모델 설정
    claude = get_builder_model(api_key)

    # 3. 에이전트 정의

    # [Agent 1] 구멍 메우기 전문 작가
    filler_writer = Agent(
        role='Gap Filler & Creative Writer',
        goal='Fill in empty descriptions and generate natural choices connecting scenes.',
        backstory='You are an expert game master. You take a skeleton structure and put flesh on it. If a user left a description blank, you write it based on the background.',
        llm=claude,
        verbose=True
    )

    # [Agent 2] 언어 및 톤앤매너 검수
    korean_editor = Agent(
        role='Korean Flavor Text Editor',
        goal='Ensure "dark/immersive" tone and correct Korean grammar.',
        backstory='You are a novelist. You fix awkward sentences and make sure the text feels like a real RPG.',
        llm=claude,
        verbose=True
    )

    # [Agent 3] 로직 검증 (Graph Consistency)
    logic_validator = Agent(
        role='Graph Logic Validator',
        goal='Ensure all graph connections defined in the draft are preserved in the final output.',
        backstory='You are a compiled code checker. You verify that `next_scene_id` in choices matches actual scene IDs.',
        llm=claude
    )

    # 4. 태스크 정의

    task_fill = Task(
        description=f"""
        Based on this skeleton draft: {json.dumps(draft_data, ensure_ascii=False)}

        1. Keep user-provided data (titles, conditions) AS IS.
        2. If 'description' is empty/short, WRITE a detailed immersive description (100+ chars).
        3. If 'choices_draft' has empty text, generate a choice text relevant to the `next_scene_id`.
        4. Generate full NPC profiles from the simple list in `global_npcs`.
        5. Write a Prologue based on the 'background'.
        """,
        agent=filler_writer,
        expected_output="JSON structure with all fields filled."
    )

    task_polish = Task(
        description="Review the filled draft. Polish Korean sentences. Make it darker and more serious.",
        agent=korean_editor,
        expected_output="Polished JSON."
    )

    task_validate = Task(
        description="""
        Final Check:
        1. Validate `next_scene_id` links.
        2. Ensure `GameScenario` schema compliance.
        """,
        agent=logic_validator,
        expected_output="Final valid JSON.",
        output_pydantic=GameScenario
    )

    # 5. 실행
    crew = Crew(
        agents=[filler_writer, korean_editor, logic_validator],
        tasks=[task_fill, task_polish, task_validate],
        verbose=True
    )

    result = crew.kickoff()

    return result.pydantic.dict()