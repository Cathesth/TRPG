"""
AI 서사 일관성 검사 서비스 (AI Audit Service)
- 씬 수정 시 이전/다음 씬과의 서사적 개연성 검토
- 선택지 트리거와 타겟 씬 내용의 일치성 검증
- LLM을 통한 논리적 흐름 분석
"""
import json
import logging
import os
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field, asdict

from llm_factory import LLMFactory, DEFAULT_MODEL

logger = logging.getLogger(__name__)


@dataclass
class NarrativeIssue:
    """서사 일관성 문제"""
    issue_type: str  # 'coherence' | 'trigger_mismatch' | 'logic_gap'
    severity: str  # 'error' | 'warning' | 'info'
    scene_id: str
    message: str
    suggestion: str = ""
    related_scene_id: str = ""
    trigger_text: str = ""


@dataclass
class AuditResult:
    """AI 감사 결과"""
    success: bool
    scene_id: str
    issues: List[NarrativeIssue] = field(default_factory=list)
    summary: str = ""
    parent_scenes: List[str] = field(default_factory=list)
    child_scenes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'success': self.success,
            'scene_id': self.scene_id,
            'issues': [asdict(issue) for issue in self.issues],
            'summary': self.summary,
            'parent_scenes': self.parent_scenes,
            'child_scenes': self.child_scenes,
            'has_errors': any(i.severity == 'error' for i in self.issues),
            'has_warnings': any(i.severity == 'warning' for i in self.issues),
            'issue_count': len(self.issues)
        }


class AIAuditService:
    """AI 기반 서사 일관성 검사 서비스"""

    # 서사 일관성 검사 프롬프트
    COHERENCE_CHECK_PROMPT = """당신은 TRPG 시나리오의 서사 전문가입니다.
주어진 씬(Scene)과 그 연결된 씬들의 서사적 일관성을 분석하세요.

## 검사 대상 씬
- ID: {scene_id}
- 제목: {scene_title}
- 내용: {scene_description}

## 이전 씬들 (이 씬으로 연결되는 씬)
{parent_scenes_info}

## 다음 씬들 (이 씬에서 연결되는 씬)
{child_scenes_info}

## 검사 항목
1. **서사적 개연성**: 이전 씬에서 현재 씬으로의 전환이 자연스러운가?
2. **논리적 연결**: 현재 씬에서 다음 씬으로의 전환이 논리적인가?
3. **분위기/톤 일관성**: 씬들 간의 분위기나 톤이 급격하게 변하지 않는가?
4. **캐릭터 행동 일관성**: 캐릭터들의 행동이 이전 맥락과 일관되는가?

## 응답 형식 (JSON)
```json
{{
    "is_coherent": true/false,
    "issues": [
        {{
            "type": "coherence|logic_gap|tone_shift|character_inconsistency",
            "severity": "error|warning|info",
            "message": "문제 설명",
            "suggestion": "개선 제안",
            "related_scene_id": "관련된 씬 ID (있는 경우)"
        }}
    ],
    "summary": "전체 평가 요약 (1-2문장)"
}}
```

서사적 문제가 없으면 issues를 빈 배열로 반환하세요.
반드시 유효한 JSON만 출력하세요.
"""

    # 트리거 일치성 검사 프롬프트
    TRIGGER_CHECK_PROMPT = """당신은 TRPG 시나리오 검수 전문가입니다.
선택지(Trigger)와 연결된 타겟 씬의 내용이 서사적으로 일치하는지 검증하세요.

## 현재 씬
- ID: {from_scene_id}
- 제목: {from_scene_title}
- 내용: {from_scene_description}

## 검사할 선택지들
{transitions_info}

## 검사 기준
1. **트리거와 결과의 일치**: 선택지를 선택했을 때 예상되는 결과와 타겟 씬의 내용이 일치하는가?
2. **맥락적 연결**: 선택지 문구가 타겟 씬의 상황과 자연스럽게 연결되는가?
3. **플레이어 기대 충족**: 플레이어가 해당 선택지를 선택했을 때 예상할 수 있는 결과인가?

## 응답 형식 (JSON)
```json
{{
    "issues": [
        {{
            "trigger": "문제가 있는 트리거 텍스트",
            "target_scene_id": "타겟 씬 ID",
            "severity": "error|warning|info",
            "message": "문제 설명",
            "suggestion": "개선 제안"
        }}
    ],
    "summary": "전체 평가 요약"
}}
```

문제가 없으면 issues를 빈 배열로 반환하세요.
반드시 유효한 JSON만 출력하세요.
"""

    @staticmethod
    def _parse_json_response(text: str) -> dict:
        """LLM 응답에서 JSON 추출"""
        if isinstance(text, dict):
            return text
        if not text:
            return {}
        try:
            text = text.strip()
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0]
            elif "```" in text:
                text = text.split("```")[1].split("```")[0]
            return json.loads(text.strip())
        except:
            try:
                start = text.find('{')
                end = text.rfind('}') + 1
                if start != -1 and end > start:
                    return json.loads(text[start:end])
            except:
                pass
        return {}

    @staticmethod
    def _get_scene_by_id(scenario_data: Dict[str, Any], scene_id: str) -> Optional[Dict[str, Any]]:
        """씬 ID로 씬 데이터 조회"""
        for scene in scenario_data.get('scenes', []):
            if scene.get('scene_id') == scene_id:
                return scene
        return None

    @staticmethod
    def _get_ending_by_id(scenario_data: Dict[str, Any], ending_id: str) -> Optional[Dict[str, Any]]:
        """엔딩 ID로 엔딩 데이터 조회"""
        for ending in scenario_data.get('endings', []):
            if ending.get('ending_id') == ending_id:
                return ending
        return None

    @staticmethod
    def _find_parent_scenes(scenario_data: Dict[str, Any], target_scene_id: str) -> List[Dict[str, Any]]:
        """특정 씬을 타겟으로 하는 부모 씬들 찾기"""
        parents = []

        # 프롤로그에서 연결되는지 확인
        prologue_connects = scenario_data.get('prologue_connects_to', [])
        if target_scene_id in prologue_connects:
            parents.append({
                'scene_id': 'PROLOGUE',
                'title': '프롤로그',
                'description': scenario_data.get('prologue', scenario_data.get('prologue_text', '')),
                'trigger': '시작'
            })

        # 다른 씬에서 연결되는지 확인
        for scene in scenario_data.get('scenes', []):
            for trans in scene.get('transitions', []):
                if trans.get('target_scene_id') == target_scene_id:
                    parents.append({
                        'scene_id': scene.get('scene_id'),
                        'title': scene.get('title') or scene.get('name') or scene.get('scene_id'),
                        'description': scene.get('description', ''),
                        'trigger': trans.get('trigger') or trans.get('condition') or '자유 행동'
                    })

        return parents

    @staticmethod
    def _find_child_scenes(scenario_data: Dict[str, Any], source_scene_id: str) -> List[Dict[str, Any]]:
        """특정 씬에서 연결되는 자식 씬/엔딩들 찾기"""
        children = []
        scene = AIAuditService._get_scene_by_id(scenario_data, source_scene_id)

        if not scene:
            return children

        for trans in scene.get('transitions', []):
            target_id = trans.get('target_scene_id')
            if not target_id:
                continue

            # 씬인지 엔딩인지 확인
            target_scene = AIAuditService._get_scene_by_id(scenario_data, target_id)
            target_ending = AIAuditService._get_ending_by_id(scenario_data, target_id)

            if target_scene:
                children.append({
                    'scene_id': target_id,
                    'title': target_scene.get('title') or target_scene.get('name') or target_id,
                    'description': target_scene.get('description', ''),
                    'trigger': trans.get('trigger') or trans.get('condition') or '자유 행동',
                    'type': 'scene'
                })
            elif target_ending:
                children.append({
                    'scene_id': target_id,
                    'title': target_ending.get('title') or target_id,
                    'description': target_ending.get('description', ''),
                    'trigger': trans.get('trigger') or trans.get('condition') or '자유 행동',
                    'type': 'ending'
                })

        return children

    @staticmethod
    def audit_scene_coherence(
        scenario_data: Dict[str, Any],
        scene_id: str,
        model_name: str = None
    ) -> AuditResult:
        """
        특정 씬의 서사 일관성 검사

        Args:
            scenario_data: 전체 시나리오 데이터
            scene_id: 검사할 씬 ID
            model_name: 사용할 LLM 모델

        Returns:
            AuditResult: 검사 결과
        """
        try:
            # 씬 정보 조회
            scene = AIAuditService._get_scene_by_id(scenario_data, scene_id)
            if not scene:
                return AuditResult(
                    success=False,
                    scene_id=scene_id,
                    summary=f"씬 '{scene_id}'을(를) 찾을 수 없습니다."
                )

            # 부모/자식 씬 찾기
            parent_scenes = AIAuditService._find_parent_scenes(scenario_data, scene_id)
            child_scenes = AIAuditService._find_child_scenes(scenario_data, scene_id)

            # 부모 씬 정보 포맷
            if parent_scenes:
                parent_info = "\n".join([
                    f"- [{p['scene_id']}] {p['title']}: {p['description'][:200]}... (트리거: \"{p['trigger']}\")"
                    for p in parent_scenes
                ])
            else:
                parent_info = "(없음 - 시작 씬일 수 있음)"

            # 자식 씬 정보 포맷
            if child_scenes:
                child_info = "\n".join([
                    f"- [{c['scene_id']}] {c['title']}: {c['description'][:200]}... (트리거: \"{c['trigger']}\")"
                    for c in child_scenes
                ])
            else:
                child_info = "(없음 - 엔딩으로 연결되거나 막다른 씬)"

            # 프롬프트 생성
            prompt = AIAuditService.COHERENCE_CHECK_PROMPT.format(
                scene_id=scene_id,
                scene_title=scene.get('title') or scene.get('name') or scene_id,
                scene_description=scene.get('description', '(설명 없음)'),
                parent_scenes_info=parent_info,
                child_scenes_info=child_info
            )

            # LLM 호출
            api_key = os.getenv("OPENROUTER_API_KEY")
            if not api_key:
                return AuditResult(
                    success=False,
                    scene_id=scene_id,
                    summary="API 키가 설정되지 않았습니다."
                )

            llm = LLMFactory.get_llm(
                model_name=model_name or DEFAULT_MODEL,
                api_key=api_key,
                temperature=0.3
            )

            response = llm.invoke(prompt)
            result_text = response.content if hasattr(response, 'content') else str(response)
            result_data = AIAuditService._parse_json_response(result_text)

            # 결과 파싱
            issues = []
            for issue_data in result_data.get('issues', []):
                issue_type = issue_data.get('type', 'coherence')
                if issue_type in ['coherence', 'logic_gap', 'tone_shift', 'character_inconsistency']:
                    issues.append(NarrativeIssue(
                        issue_type=issue_type,
                        severity=issue_data.get('severity', 'warning'),
                        scene_id=scene_id,
                        message=issue_data.get('message', ''),
                        suggestion=issue_data.get('suggestion', ''),
                        related_scene_id=issue_data.get('related_scene_id', '')
                    ))

            return AuditResult(
                success=True,
                scene_id=scene_id,
                issues=issues,
                summary=result_data.get('summary', '검사 완료'),
                parent_scenes=[p['scene_id'] for p in parent_scenes],
                child_scenes=[c['scene_id'] for c in child_scenes]
            )

        except Exception as e:
            logger.error(f"Coherence audit error: {e}", exc_info=True)
            return AuditResult(
                success=False,
                scene_id=scene_id,
                summary=f"검사 중 오류 발생: {str(e)}"
            )

    @staticmethod
    def audit_trigger_consistency(
        scenario_data: Dict[str, Any],
        scene_id: str,
        model_name: str = None
    ) -> AuditResult:
        """
        씬의 선택지(트리거)와 타겟 씬 내용의 일치성 검사

        Args:
            scenario_data: 전체 시나리오 데이터
            scene_id: 검사할 씬 ID
            model_name: 사용할 LLM 모델

        Returns:
            AuditResult: 검사 결과
        """
        try:
            scene = AIAuditService._get_scene_by_id(scenario_data, scene_id)
            if not scene:
                return AuditResult(
                    success=False,
                    scene_id=scene_id,
                    summary=f"씬 '{scene_id}'을(를) 찾을 수 없습니다."
                )

            transitions = scene.get('transitions', [])
            if not transitions:
                return AuditResult(
                    success=True,
                    scene_id=scene_id,
                    summary="이 씬에는 검사할 선택지가 없습니다."
                )

            # 전환 정보 수집
            transitions_info_list = []
            for trans in transitions:
                target_id = trans.get('target_scene_id')
                trigger = trans.get('trigger') or trans.get('condition') or '자유 행동'

                target_scene = AIAuditService._get_scene_by_id(scenario_data, target_id)
                target_ending = AIAuditService._get_ending_by_id(scenario_data, target_id)

                if target_scene:
                    target_info = f"[씬] {target_scene.get('title', target_id)}: {target_scene.get('description', '')[:300]}"
                elif target_ending:
                    target_info = f"[엔딩] {target_ending.get('title', target_id)}: {target_ending.get('description', '')[:300]}"
                else:
                    target_info = f"[알 수 없음] {target_id}"

                transitions_info_list.append(
                    f"선택지: \"{trigger}\"\n  → 타겟: {target_info}"
                )

            transitions_info = "\n\n".join(transitions_info_list)

            # 프롬프트 생성
            prompt = AIAuditService.TRIGGER_CHECK_PROMPT.format(
                from_scene_id=scene_id,
                from_scene_title=scene.get('title') or scene.get('name') or scene_id,
                from_scene_description=scene.get('description', '(설명 없음)'),
                transitions_info=transitions_info
            )

            # LLM 호출
            api_key = os.getenv("OPENROUTER_API_KEY")
            if not api_key:
                return AuditResult(
                    success=False,
                    scene_id=scene_id,
                    summary="API 키가 설정되지 않았습니다."
                )

            llm = LLMFactory.get_llm(
                model_name=model_name or DEFAULT_MODEL,
                api_key=api_key,
                temperature=0.3
            )

            response = llm.invoke(prompt)
            result_text = response.content if hasattr(response, 'content') else str(response)
            result_data = AIAuditService._parse_json_response(result_text)

            # 결과 파싱
            issues = []
            for issue_data in result_data.get('issues', []):
                issues.append(NarrativeIssue(
                    issue_type='trigger_mismatch',
                    severity=issue_data.get('severity', 'warning'),
                    scene_id=scene_id,
                    message=issue_data.get('message', ''),
                    suggestion=issue_data.get('suggestion', ''),
                    related_scene_id=issue_data.get('target_scene_id', ''),
                    trigger_text=issue_data.get('trigger', '')
                ))

            return AuditResult(
                success=True,
                scene_id=scene_id,
                issues=issues,
                summary=result_data.get('summary', '트리거 검사 완료'),
                child_scenes=[t.get('target_scene_id') for t in transitions if t.get('target_scene_id')]
            )

        except Exception as e:
            logger.error(f"Trigger audit error: {e}", exc_info=True)
            return AuditResult(
                success=False,
                scene_id=scene_id,
                summary=f"검사 중 오류 발생: {str(e)}"
            )

    @staticmethod
    def full_audit(
        scenario_data: Dict[str, Any],
        scene_id: str,
        model_name: str = None
    ) -> Dict[str, Any]:
        """
        전체 AI 감사 수행 (서사 일관성 + 트리거 일치성)

        Args:
            scenario_data: 전체 시나리오 데이터
            scene_id: 검사할 씬 ID
            model_name: 사용할 LLM 모델

        Returns:
            통합 검사 결과
        """
        coherence_result = AIAuditService.audit_scene_coherence(scenario_data, scene_id, model_name)
        trigger_result = AIAuditService.audit_trigger_consistency(scenario_data, scene_id, model_name)

        # 결과 통합
        all_issues = coherence_result.issues + trigger_result.issues
        has_errors = any(i.severity == 'error' for i in all_issues)
        has_warnings = any(i.severity == 'warning' for i in all_issues)

        return {
            'success': coherence_result.success and trigger_result.success,
            'scene_id': scene_id,
            'coherence': coherence_result.to_dict(),
            'trigger': trigger_result.to_dict(),
            'total_issues': len(all_issues),
            'has_errors': has_errors,
            'has_warnings': has_warnings,
            'summary': f"서사 검사: {coherence_result.summary} | 트리거 검사: {trigger_result.summary}"
        }

