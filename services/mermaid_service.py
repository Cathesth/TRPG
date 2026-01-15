"""
Mermaid 차트 생성 서비스
"""
import logging
from typing import Dict, Any, List, Union, Tuple

logger = logging.getLogger(__name__)

class MermaidService:
    """시나리오를 Mermaid 다이어그램으로 변환"""

    @staticmethod
    def convert_nodes_to_scenes(nodes: List[Dict], edges: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
        """
        React Flow 노드/엣지 데이터를 시나리오 씬/엔딩 구조로 변환
        Builder(노드 기반) -> Game Engine(씬 기반) 호환성 보장
        """
        scenes = []
        endings = []

        # 1. 노드 분류
        node_map = {n['id']: n for n in nodes}

        for node in nodes:
            if node['type'] == 'scene':
                # React Flow 노드 데이터를 씬 데이터로 변환
                scene = {
                    'scene_id': node['id'],
                    'title': node['data'].get('title', node['data'].get('label', '')),
                    'description': node['data'].get('description', node['data'].get('prologue', '')),
                    'trigger': node['data'].get('trigger', ''),
                    'transitions': []
                }
                # 추가 속성이 있다면 포함 (예: npcs, enemies)
                if 'npcs' in node['data']:
                    scene['npcs'] = node['data']['npcs']
                if 'enemies' in node['data']:
                    scene['enemies'] = node['data']['enemies']

                scenes.append(scene)
            elif node['type'] == 'ending':
                ending = {
                    'ending_id': node['id'],
                    'title': node['data'].get('title', ''),
                    'description': node['data'].get('description', '')
                }
                endings.append(ending)

        # 2. 엣지로 Transitions 구성
        for edge in edges:
            source_id = edge.get('source')
            target_id = edge.get('target')

            source_node = node_map.get(source_id)
            target_node = node_map.get(target_id)

            if not source_node or not target_node:
                continue

            # Start 노드에서 시작하는 경우 (Prologue 연결)
            # 보통 Start 노드는 별도 처리가 필요할 수 있으나, 여기서는 엣지 구조만 파악

            if source_node['type'] == 'scene':
                # 해당 씬 찾기
                scene = next((s for s in scenes if s['scene_id'] == source_id), None)
                if scene:
                    target_trigger = ''
                    # 타겟 노드의 트리거 정보를 가져옴 (조건)
                    if target_node['type'] == 'scene':
                        target_trigger = target_node['data'].get('trigger', '')

                    scene['transitions'].append({
                        'target_scene_id': target_id,
                        'trigger': target_trigger or '이동'
                    })

        return scenes, endings

    @staticmethod
    def generate_chart(scenario: Union[Dict, Any], current_scene_id: str = None) -> Dict[str, Any]:
        """
        시나리오 데이터로부터 Mermaid 차트와 관련 정보 생성

        Args:
            scenario: 시나리오 데이터 딕셔너리 또는 Scenario 객체
            current_scene_id: 현재 활성화된 씬 ID (하이라이트용)

        Returns:
            {
                'mermaid_code': str,
                'filtered_scenes': List,
                ...
            }
        """
        try:
            # 입력 데이터 정규화 (Dict로 변환)
            if hasattr(scenario, 'data') and isinstance(scenario.data, dict):
                # Scenario 모델 객체인 경우
                scenario_data = scenario.data.get('scenario', scenario.data)
            elif isinstance(scenario, dict):
                # 딕셔너리인 경우 (Draft 데이터 등)
                scenario_data = scenario
            else:
                return {"mermaid_code": "graph TD\nError[데이터 형식 오류]"}

            scenes = scenario_data.get('scenes', [])
            endings = scenario_data.get('endings', [])
            nodes = scenario_data.get('nodes', [])
            edges = scenario_data.get('edges', [])

            # [핵심] scenes가 없지만 nodes가 있는 경우 자동 변환 (Viewer 호환성)
            if (not scenes or len(scenes) == 0) and nodes:
                scenes, endings = MermaidService.convert_nodes_to_scenes(nodes, edges)
                # 변환된 데이터를 원본 딕셔너리에도 반영 (참조 변경)
                scenario_data['scenes'] = scenes
                scenario_data['endings'] = endings

            # start/PROLOGUE 노드 제외
            filtered_scenes = [
                s for s in scenes
                if s.get('scene_id') not in ('start', 'PROLOGUE')
            ]

            mermaid_lines = ["graph TD"]
            prologue_text = scenario_data.get('prologue', scenario_data.get('prologue_text', ''))
            prologue_connects_to = scenario_data.get('prologue_connects_to', [])

            # prologue_connects_to가 없으면 자동 탐지
            if not prologue_connects_to and filtered_scenes:
                all_target_ids = set()
                for scene in filtered_scenes:
                    for trans in scene.get('transitions', []):
                        target_id = trans.get('target_scene_id')
                        if target_id:
                            all_target_ids.add(target_id)

                root_scenes = [
                    scene.get('scene_id')
                    for scene in filtered_scenes
                    if scene.get('scene_id') not in all_target_ids
                ]
                prologue_connects_to = root_scenes if root_scenes else [filtered_scenes[0].get('scene_id')]

            # 매핑 생성
            ending_names = {e.get('ending_id'): e.get('title', e.get('ending_id')) for e in endings}
            scene_names = {s.get('scene_id'): s.get('title') or s.get('name') or s.get('scene_id') for s in filtered_scenes}

            # 표시용 ID 생성
            scene_display_ids = {}
            for idx, scene in enumerate(filtered_scenes):
                scene_display_ids[scene.get('scene_id')] = f"Scene-{idx + 1}"

            ending_display_ids = {}
            for idx, ending in enumerate(endings):
                ending_display_ids[ending.get('ending_id')] = f"Ending-{idx + 1}"

            # incoming conditions 계산
            incoming_conditions = {}
            ending_incoming_conditions = {}

            # 프롤로그 연결
            for target_id in prologue_connects_to:
                if target_id not in incoming_conditions:
                    incoming_conditions[target_id] = []
                incoming_conditions[target_id].append({
                    'from_scene': 'PROLOGUE',
                    'from_title': '프롤로그',
                    'condition': '게임 시작'
                })

            # 씬 간 transitions
            for scene in filtered_scenes:
                from_id = scene.get('scene_id')
                from_title = scene.get('title', from_id)

                for trans in scene.get('transitions', []):
                    target_id = trans.get('target_scene_id')
                    if not target_id: continue

                    condition_info = {
                        'from_scene': from_id,
                        'from_title': from_title,
                        'condition': trans.get('trigger') or trans.get('condition') or '자유 행동'
                    }

                    if target_id in ending_names:
                        if target_id not in ending_incoming_conditions:
                            ending_incoming_conditions[target_id] = []
                        ending_incoming_conditions[target_id].append(condition_info)
                    else:
                        if target_id not in incoming_conditions:
                            incoming_conditions[target_id] = []
                        incoming_conditions[target_id].append(condition_info)

            # Mermaid 코드 생성
            if prologue_text:
                # 프롤로그는 기본 스타일만 적용 (JavaScript에서 하이라이트 처리)
                # 하이라이트 시 class 적용을 위해 ID는 'PROLOGUE'로 고정
                prologue_class = "active" if current_scene_id and current_scene_id.lower() == "prologue" else "prologueStyle"
                mermaid_lines.append(f'    PROLOGUE["📖 Prologue"]:::{prologue_class}')

            if prologue_text and prologue_connects_to:
                for target_id in prologue_connects_to:
                    if any(s.get('scene_id') == target_id for s in filtered_scenes):
                        mermaid_lines.append(f'    PROLOGUE --> {target_id}')

            for scene in filtered_scenes:
                scene_id = scene['scene_id']
                scene_title = (scene.get('title') or scene.get('name') or scene_id).replace('"', "'")

                node_class = "active" if current_scene_id == scene_id else "sceneStyle"
                mermaid_lines.append(f'    {scene_id}["{scene_title}"]:::{node_class}')

                for trans in scene.get('transitions', []):
                    next_id = trans.get('target_scene_id')
                    trigger = (trans.get('trigger') or 'action').replace('"', "'")
                    if next_id and next_id != 'start':
                        mermaid_lines.append(f'    {scene_id} -->|"{trigger}"| {next_id}')

            for ending in endings:
                ending_id = ending['ending_id']
                ending_title = ending.get('title', '엔딩').replace('"', "'")

                node_class = "active" if current_scene_id == ending_id else "endingStyle"
                mermaid_lines.append(f'    {ending_id}["🏁 {ending_title}"]:::{node_class}')

            mermaid_lines.append("    classDef default fill:#1f2937,stroke:#374151,stroke-width:2px,color:#fff")
            mermaid_lines.append("    classDef active fill:#164e63,stroke:#22d3ee,stroke-width:3px,color:#fff")
            mermaid_lines.append("    classDef prologueStyle fill:#0f766e,stroke:#14b8a6,color:#fff")
            mermaid_lines.append("    classDef sceneStyle fill:#312e81,stroke:#6366f1,color:#fff")
            mermaid_lines.append("    classDef endingStyle fill:#831843,stroke:#ec4899,color:#fff")

            return {
                'mermaid_code': "\n".join(mermaid_lines),
                'filtered_scenes': filtered_scenes,
                'incoming_conditions': incoming_conditions,
                'ending_incoming_conditions': ending_incoming_conditions,
                'ending_names': ending_names,
                'scene_names': scene_names,
                'scene_display_ids': scene_display_ids,
                'ending_display_ids': ending_display_ids
            }

        except Exception as e:
            logger.error(f"Mermaid generation error: {e}")
            return {"mermaid_code": "graph TD\nError[차트 생성 실패]"}

    @staticmethod
    def _escape(text: str) -> str:
        """Mermaid 문법 파괴 방지를 위한 이스케이프"""
        if not text: return ""
        return text.replace('"', "'").replace('\n', ' ').replace('\r', '')

    @staticmethod
    def generate_mermaid_from_scenario(scenario_data: dict) -> str:
        """
        ✅ [FIX 2-A] generate_chart 래퍼 메서드 - 호환성 유지

        routes/views.py의 view_debug_scenes에서 호출하는 메서드
        generate_chart를 호출하고 mermaid_code만 추출하여 반환

        Args:
            scenario_data: 시나리오 데이터 딕셔너리

        Returns:
            Mermaid 코드 문자열
        """
        try:
            # scenario_data 구조 확인 및 unwrap
            if isinstance(scenario_data, dict):
                # 'data' 필드로 감싸진 경우 unwrap
                if 'data' in scenario_data and isinstance(scenario_data['data'], dict):
                    unwrapped = scenario_data['data']
                    # 'scenario' 필드가 있으면 한번 더 unwrap
                    if 'scenario' in unwrapped and isinstance(unwrapped['scenario'], dict):
                        scenario_data = unwrapped['scenario']
                    else:
                        scenario_data = unwrapped
                # 'scenario' 필드로 감싸진 경우 unwrap
                elif 'scenario' in scenario_data and isinstance(scenario_data['scenario'], dict):
                    scenario_data = scenario_data['scenario']

            # generate_chart 호출
            result = MermaidService.generate_chart(scenario_data)

            # mermaid_code 추출
            if isinstance(result, dict) and 'mermaid_code' in result:
                logger.info(f"✅ [MERMAID] Successfully generated chart from scenario")
                return result['mermaid_code']
            else:
                logger.warning(f"⚠️ [MERMAID] generate_chart returned unexpected format")
                return "graph TD\n    A[차트 생성 실패]"

        except Exception as e:
            logger.error(f"❌ [MERMAID] generate_mermaid_from_scenario failed: {e}", exc_info=True)
            return "graph TD\n    Error[차트 생성 중 오류 발생]"
