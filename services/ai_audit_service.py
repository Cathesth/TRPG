"""
AI 서사 일관성 검사 서비스 (AI Audit Service)
- 씬 수정 시 이전/다음 씬과의 서사적 개연성 검토
- 선택지 트리거와 타겟 씬 내용의 일치성 검증
- LLM을 통한 논리적 흐름 분석 및 수정 제안
"""
import json
import logging
import os
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field, asdict

# LLM Factory 연동
try:
    from llm_factory import LLMFactory, DEFAULT_MODEL
except ImportError:
    from llm_factory import LLMFactory
    # fallback default model if not defined in factory
    DEFAULT_MODEL = "openai/tngtech/deepseek-r1t2-chimera:free"

logger = logging.getLogger(__name__)


@dataclass
class NarrativeIssue:
    """서사 일관성 문제 항목"""
    issue_type: str  # 'coherence' | 'trigger_mismatch' | 'logic_gap'
    severity: str    # 'error' | 'warning' | 'info'
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
        """결과를 딕셔너리로 변환 (API 응답용)"""
        return {
            'success': self.success,
            'scene_id': self.scene_id,
            # dataclass 객체를 dict로 변환
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

    # --- Prompts ---

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

    AUDIT_RECOMMENDATION_PROMPT = """당신은 TRPG 시나리오의 구조적 결함을 찾아내는 수석 에디터입니다.
주어진 시나리오의 '구조 데이터'를 분석하여, 서사적 오류나 개연성 문제가 의심되어 **정밀 검수(Audit)가 가장 시급한 씬 3~5개**를 추천하세요.

## 분석 대상 시나리오 구조
{scenario_structure}

## 추천 기준 (우선순위)
1. **단절/고립**: 연결이 끊겼거나 진입/탈출이 불가능해 보이는 씬
2. **복잡성**: 분기점이 너무 많아(3개 이상) 로직 꼬임이 의심되는 씬
3. **내용 부실**: 묘사가 지나치게 짧거나('내용 없음' 등) 핵심 정보가 누락된 씬
4. **급격한 전개**: 초반부에서 갑자기 엔딩으로 직행하는 등 템포가 이상한 구간

## 응답 형식 (JSON)
```json
{{
    "recommendations": [
        {{
            "scene_id": "추천할 씬 ID",
            "reason": "왜 이 씬을 검수해야 하는지 구체적인 이유 (한글로)",
            "risk_level": "High|Medium|Low"
        }}
    ]
}}
```
반드시 유효한 JSON만 출력하세요.
"""

    # --- Helper Methods ---

    @staticmethod
    def _parse_json_response(text: str) -> dict:
        """LLM 응답에서 JSON 추출 및 파싱"""
        if isinstance(text, dict):
            return text
        if not text:
            return {}

        try:
            text = text.strip()
            # Markdown 코드 블록 제거
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0]
            elif "```" in text:
                text = text.split("```")[1].split("```")[0]

            return json.loads(text.strip())
        except json.JSONDecodeError:
            # 단순 파싱 실패 시, 중괄호 찾아서 재시도
            try:
                start = text.find('{')
                end = text.rfind('}') + 1
                if start != -1 and end > start:
                    return json.loads(text[start:end])
            except:
                pass
            logger.warning(f"Failed to parse JSON response: {text[:100]}...")
            return {}

    @staticmethod
    def _get_scene_by_id(scenario_data: Dict[str, Any], scene_id: str) -> Optional[Dict[str, Any]]:
        for scene in scenario_data.get('scenes', []):
            if scene.get('scene_id') == scene_id:
                return scene
        return None

    @staticmethod
    def _get_ending_by_id(scenario_data: Dict[str, Any], ending_id: str) -> Optional[Dict[str, Any]]:
        for ending in scenario_data.get('endings', []):
            if ending.get('ending_id') == ending_id:
                return ending
        return None

    @staticmethod
    def _find_parent_scenes(scenario_data: Dict[str, Any], target_scene_id: str) -> List[Dict[str, Any]]:
        parents = []
        # 프롤로그 연결 확인
        if target_scene_id in scenario_data.get('prologue_connects_to', []):
            parents.append({
                'scene_id': 'PROLOGUE',
                'title': '프롤로그',
                'description': scenario_data.get('prologue', scenario_data.get('prologue_text', '')),
                'trigger': '시작'
            })

        # 씬 연결 확인
        for scene in scenario_data.get('scenes', []):
            for trans in scene.get('transitions', []):
                if trans.get('target_scene_id') == target_scene_id:
                    parents.append({
                        'scene_id': scene.get('scene_id'),
                        'title': scene.get('title') or scene.get('scene_id'),
                        'description': scene.get('description', ''),
                        'trigger': trans.get('trigger') or trans.get('condition') or '자유 행동'
                    })
        return parents

    @staticmethod
    def _find_child_scenes(scenario_data: Dict[str, Any], source_scene_id: str) -> List[Dict[str, Any]]:
        children = []
        scene = AIAuditService._get_scene_by_id(scenario_data, source_scene_id)
        if not scene:
            return children

        for trans in scene.get('transitions', []):
            target_id = trans.get('target_scene_id')
            if not target_id: continue

            target_scene = AIAuditService._get_scene_by_id(scenario_data, target_id)
            target_ending = AIAuditService._get_ending_by_id(scenario_data, target_id)

            if target_scene:
                children.append({
                    'scene_id': target_id,
                    'title': target_scene.get('title') or target_id,
                    'description': target_scene.get('description', ''),
                    'trigger': trans.get('trigger') or '자유 행동',
                    'type': 'scene'
                })
            elif target_ending:
                children.append({
                    'scene_id': target_id,
                    'title': target_ending.get('title') or target_id,
                    'description': target_ending.get('description', ''),
                    'trigger': trans.get('trigger') or '자유 행동',
                    'type': 'ending'
                })
        return children

    # --- Core Audit Methods ---

    @staticmethod
    def audit_scene_coherence(
        scenario_data: Dict[str, Any],
        scene_id: str,
        model_name: str = None
    ) -> AuditResult:
        """씬의 전후 연결성(개연성) 검사"""
        try:
            scene = AIAuditService._get_scene_by_id(scenario_data, scene_id)
            if not scene:
                return AuditResult(success=False, scene_id=scene_id, summary="씬을 찾을 수 없습니다.")

            parent_scenes = AIAuditService._find_parent_scenes(scenario_data, scene_id)
            child_scenes = AIAuditService._find_child_scenes(scenario_data, scene_id)

            # 프롬프트 구성용 정보 생성
            parent_info = "\n".join([
                f"- [{p['scene_id']}] {p['title']}: {p['description'][:200]}... (트리거: \"{p['trigger']}\")"
                for p in parent_scenes
            ]) if parent_scenes else "(없음 - 시작점 가능성)"

            child_info = "\n".join([
                f"- [{c['scene_id']}] {c['title']}: {c['description'][:200]}... (트리거: \"{c['trigger']}\")"
                for c in child_scenes
            ]) if child_scenes else "(없음 - 엔딩 또는 고립)"

            prompt = AIAuditService.COHERENCE_CHECK_PROMPT.format(
                scene_id=scene_id,
                scene_title=scene.get('title') or scene_id,
                scene_description=scene.get('description', ''),
                parent_scenes_info=parent_info,
                child_scenes_info=child_info
            )

            # LLM 호출 (토큰 측정 포함)
            api_key = os.getenv("OPENROUTER_API_KEY")
            if not api_key:
                return AuditResult(success=False, scene_id=scene_id, summary="API Key Missing")

            llm = LLMFactory.get_llm(
                model_name=model_name or DEFAULT_MODEL,
                api_key=api_key,
                temperature=0.3
            )
            
            # 토큰 사용량 측정
            with get_openai_callback() as cb:
                response = llm.invoke(prompt)
                result_data = AIAuditService._parse_json_response(
                    response.content if hasattr(response, 'content') else str(response)
                )
                
                # 토큰 사용량 정보 저장
                prompt_tokens = cb.prompt_tokens
                completion_tokens = cb.completion_tokens
                total_tokens = prompt_tokens + completion_tokens
                
                logger.info(f"[AUDIT TOKENS] Model: {model_name}, Scene: {scene_id}, Tokens: {total_tokens}")
                
                # 토큰 정보를 결과에 추가
                result_data['token_usage'] = {
                    'prompt_tokens': prompt_tokens,
                    'completion_tokens': completion_tokens,
                    'total_tokens': total_tokens
                }

            issues = []
            for issue in result_data.get('issues', []):
                issues.append(NarrativeIssue(
                    issue_type=issue.get('type', 'coherence'),
                    severity=issue.get('severity', 'warning'),
                    scene_id=scene_id,
                    message=issue.get('message', ''),
                    suggestion=issue.get('suggestion', ''),
                    related_scene_id=issue.get('related_scene_id', '')
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
            logger.error(f"Audit Coherence Error: {e}", exc_info=True)
            return AuditResult(success=False, scene_id=scene_id, summary=str(e))

    @staticmethod
    def audit_trigger_consistency(
        scenario_data: Dict[str, Any],
        scene_id: str,
        model_name: str = None
    ) -> AuditResult:
        """선택지와 타겟 씬의 내용 일치성 검사"""
        try:
            scene = AIAuditService._get_scene_by_id(scenario_data, scene_id)
            if not scene:
                return AuditResult(success=False, scene_id=scene_id, summary="씬을 찾을 수 없습니다.")

            transitions = scene.get('transitions', [])
            if not transitions:
                return AuditResult(success=True, scene_id=scene_id, summary="검사할 선택지가 없습니다.")

            trans_info_list = []
            for t in transitions:
                tid = t.get('target_scene_id')
                trigger = t.get('trigger') or t.get('condition') or '이동'

                t_scene = AIAuditService._get_scene_by_id(scenario_data, tid)
                t_ending = AIAuditService._get_ending_by_id(scenario_data, tid)

                desc = ""
                title = tid
                if t_scene:
                    title = t_scene.get('title')
                    desc = t_scene.get('description', '')[:300]
                elif t_ending:
                    title = t_ending.get('title')
                    desc = t_ending.get('description', '')[:300]

                trans_info_list.append(f"선택지: \"{trigger}\" -> 타겟: [{title}] {desc}")

            prompt = AIAuditService.TRIGGER_CHECK_PROMPT.format(
                from_scene_id=scene_id,
                from_scene_title=scene.get('title') or scene_id,
                from_scene_description=scene.get('description', ''),
                transitions_info="\n".join(trans_info_list)
            )

            api_key = os.getenv("OPENROUTER_API_KEY")
            llm = LLMFactory.get_llm(model_name=model_name or DEFAULT_MODEL, api_key=api_key, temperature=0.3)
            response = llm.invoke(prompt)
            result_data = AIAuditService._parse_json_response(
                response.content if hasattr(response, 'content') else str(response)
            )

            issues = []
            for issue in result_data.get('issues', []):
                issues.append(NarrativeIssue(
                    issue_type='trigger_mismatch',
                    severity=issue.get('severity', 'warning'),
                    scene_id=scene_id,
                    message=issue.get('message', ''),
                    suggestion=issue.get('suggestion', ''),
                    related_scene_id=issue.get('target_scene_id', ''),
                    trigger_text=issue.get('trigger', '')
                ))

            return AuditResult(
                success=True,
                scene_id=scene_id,
                issues=issues,
                summary=result_data.get('summary', '트리거 검사 완료'),
                child_scenes=[t.get('target_scene_id') for t in transitions if t.get('target_scene_id')]
            )

        except Exception as e:
            logger.error(f"Audit Trigger Error: {e}", exc_info=True)
            return AuditResult(success=False, scene_id=scene_id, summary=str(e))

    @staticmethod
    def full_audit(
        scenario_data: Dict[str, Any],
        scene_id: str,
        model_name: str = None
    ) -> Dict[str, Any]:
        """통합 검사 수행"""
        coherence = AIAuditService.audit_scene_coherence(scenario_data, scene_id, model_name)
        trigger = AIAuditService.audit_trigger_consistency(scenario_data, scene_id, model_name)

        all_issues = coherence.issues + trigger.issues

        return {
            'success': coherence.success and trigger.success,
            'scene_id': scene_id,
            'coherence': coherence.to_dict(),
            'trigger': trigger.to_dict(),
            'total_issues': len(all_issues),
            'has_errors': any(i.severity == 'error' for i in all_issues),
            'has_warnings': any(i.severity == 'warning' for i in all_issues),
            'summary': f"{coherence.summary} / {trigger.summary}"
        }

    @staticmethod
    def recommend_audit_targets(
        scenario_data: Dict[str, Any],
        model_name: str = None
    ) -> Dict[str, Any]:
        """[신규] 전체 시나리오 구조 분석 및 검수 대상 추천"""
        try:
            scenes = scenario_data.get('scenes', [])
            endings = scenario_data.get('endings', [])

            # 구조 요약 (토큰 절약)
            summary_lines = []
            for s in scenes:
                trans_cnt = len(s.get('transitions', []))
                desc_len = len(s.get('description', ''))
                targets = [t.get('target_scene_id') for t in s.get('transitions', []) if t.get('target_scene_id')]
                summary_lines.append(
                    f"- ID:{s['scene_id']} | T:{s.get('title','')} | Len:{desc_len} | Branch:{trans_cnt} | To:{targets}"
                )
            for e in endings:
                summary_lines.append(f"- [END] ID:{e['ending_id']} | T:{e.get('title','')}")

            prompt = AIAuditService.AUDIT_RECOMMENDATION_PROMPT.format(
                scenario_structure="\n".join(summary_lines)
            )

            api_key = os.getenv("OPENROUTER_API_KEY")
            if not api_key:
                return {"success": False, "error": "API Key Missing"}

            llm = LLMFactory.get_llm(model_name=model_name or DEFAULT_MODEL, api_key=api_key, temperature=0.1)
            response = llm.invoke(prompt)
            result_data = AIAuditService._parse_json_response(
                response.content if hasattr(response, 'content') else str(response)
            )

            return {
                "success": True,
                "recommendations": result_data.get('recommendations', []),
                "analyzed_count": len(scenes)
            }

        except Exception as e:
            logger.error(f"Audit Recommendation Error: {e}", exc_info=True)
            return {"success": False, "error": str(e)}