"""
Mermaid 차트 생성 서비스
"""
from typing import Dict, Any, List


class MermaidService:
    """시나리오를 Mermaid 다이어그램으로 변환"""

    @staticmethod
    def generate_chart(scenario: Dict[str, Any], current_scene_id: str = None) -> Dict[str, Any]:
        """
        시나리오 데이터로부터 Mermaid 차트와 관련 정보 생성

        Returns:
            {
                'mermaid_code': str,
                'filtered_scenes': List,
                'incoming_conditions': Dict,
                'ending_incoming_conditions': Dict,
                'ending_names': Dict,
                'scene_names': Dict,
                'scene_display_ids': Dict,  # scene_id -> Scene-1, Scene-2, ...
                'ending_display_ids': Dict  # ending_id -> Ending-1, Ending-2, ...
            }
        """
        scenes = scenario.get('scenes', [])
        endings = scenario.get('endings', [])

        # start/PROLOGUE 노드 제외
        filtered_scenes = [
            s for s in scenes
            if s.get('scene_id') not in ('start', 'PROLOGUE')
        ]

        mermaid_lines = ["graph TD"]
        prologue_text = scenario.get('prologue', scenario.get('prologue_text', ''))
        prologue_connects_to = scenario.get('prologue_connects_to', [])

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
        scene_names = {s.get('scene_id'): s.get('title', s.get('scene_id')) for s in filtered_scenes}

        # 표시용 ID 생성 (Scene-1, Scene-2, ... / Ending-1, Ending-2, ...)
        scene_display_ids = {}
        for idx, scene in enumerate(filtered_scenes):
            scene_display_ids[scene.get('scene_id')] = f"Scene-{idx + 1}"

        ending_display_ids = {}
        for idx, ending in enumerate(endings):
            ending_display_ids[ending.get('ending_id')] = f"Ending-{idx + 1}"

        # incoming conditions 계산
        incoming_conditions = {}
        ending_incoming_conditions = {}

        # 프롤로그에서 시작하는 씬들
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
                if not target_id:
                    continue

                condition_info = {
                    'from_scene': from_id,
                    'from_title': from_title,
                    'condition': trans.get('trigger') or trans.get('condition') or '자유 행동'
                }

                # 엔딩으로의 연결인지 확인
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
            mermaid_lines.append(f'    PROLOGUE["📖 Prologue"]:::prologueStyle')

        # 프롤로그 -> 연결된 씬들
        if prologue_text and prologue_connects_to:
            for target_id in prologue_connects_to:
                if any(s.get('scene_id') == target_id for s in filtered_scenes):
                    mermaid_lines.append(f'    PROLOGUE --> {target_id}')

        # 씬 노드들
        for scene in filtered_scenes:
            scene_id = scene['scene_id']
            scene_title = scene.get('title', scene_id).replace('"', "'")

            # Scene title을 노드 레이블로 사용
            mermaid_lines.append(f'    {scene_id}["{scene_title}"]:::sceneStyle')

            for trans in scene.get('transitions', []):
                next_id = trans.get('target_scene_id')
                trigger = trans.get('trigger', 'action').replace('"', "'")
                if next_id and next_id != 'start':
                    mermaid_lines.append(f'    {scene_id} -->|"{trigger}"| {next_id}')

        # 엔딩 노드들
        for ending in endings:
            ending_id = ending['ending_id']
            ending_title = ending.get('title', '엔딩').replace('"', "'")

            # 기본 스타일만 적용 (JavaScript에서 하이라이트 처리)
            mermaid_lines.append(f'    {ending_id}["🏁 {ending_title}"]:::endingStyle')

        # 스타일 정의
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
