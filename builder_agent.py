from crewai import Agent, Task, Crew
from schemas import GameScenario
from llm_factory import get_builder_model
import json


def generate_scenario_data(api_key: str, draft_data: dict):
    """
    작가 -> 언어 검수 -> 논리 검수 순서로 완벽한 시나리오 생성
    """
    claude = get_builder_model(api_key)

    # 1. [Agent] 시나리오 작가 (창조)
    writer = Agent(
        role='Lead Scenario Writer',
        goal='Draft a creative and immersive TRPG scenario.',
        backstory='You are a creative writer who excels at creating interesting characters and plot twists.',
        llm=claude,
        verbose=True
    )

    # 2. [Agent] 언어 검수자 (표현력)
    language_editor = Agent(
        role='Korean Language Editor',
        goal='Ensure the text is in natural, high-quality Korean and matches the requested tone.',
        backstory='You are a strict editor. You check for grammar, tone consistency, and ensure the text is immersive for players.',
        llm=claude,
        verbose=True
    )

    # 3. [Agent] 시나리오 검수자 (논리/구조)
    logic_critic = Agent(
        role='Game Logic Validator',
        goal='Ensure the scenario graph is logically connected without dead ends or broken links.',
        backstory='''You are a QA Engineer. You check:
        1. Does every scene with choices link to a valid `scene_id`?
        2. Is there at least one path to an Ending?
        3. Are all NPC details fully filled out?
        If you find a logic error, you fix the JSON structure.''',
        llm=claude,
        verbose=True
    )

    # --- Tasks ---

    draft_json = json.dumps(draft_data, indent=2, ensure_ascii=False)

    # Task 1: 초안 작성
    task_write = Task(
        description=f"""
        Create a full TRPG scenario based on this draft:
        {draft_json}

        Fill in all missing fields (prologue, scenes, npcs, endings).
        Ensure `initial_state` has necessary variables.
        """,
        agent=writer,
        expected_output="A JSON-like draft of the scenario."
    )

    # Task 2: 언어 및 묘사 검수
    task_edit = Task(
        description="""
        Review the draft from the Writer.
        1. Ensure all text is in fluent Korean.
        2. Enhance scene descriptions to be more vivid.
        3. Check if NPC dialogues match their personalities.
        """,
        agent=language_editor,
        expected_output="Refined JSON data with better text."
    )

    # Task 3: 논리 검수 및 최종 출력 (가장 중요)
    task_validate = Task(
        description="""
        Review the refined draft for LOGICAL ERRORS.
        1. Check `next_scene_id` validity: Every ID must exist in `scenes` or `endings`.
        2. Check Flow: Prologue -> Scenes -> Ending flow must be possible.
        3. Output MUST be a valid JSON matching the `GameScenario` schema perfectly.
        """,
        agent=logic_critic,
        expected_output="Final valid JSON object matching GameScenario schema.",
        output_pydantic=GameScenario  # 최종 포맷 강제
    )

    # Process: Sequential (작성 -> 언어검수 -> 논리검수 -> 완료)
    crew = Crew(
        agents=[writer, language_editor, logic_critic],
        tasks=[task_write, task_edit, task_validate],
        verbose=True
    )

    result = crew.kickoff()

    return result.pydantic.dict()