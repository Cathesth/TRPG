from pydantic import BaseModel, Field
from typing import List, Dict, Union, Literal, Optional, Any


# 1. 효과(Effect) 정의
class Effect(BaseModel):
    target_var: str = Field(description="변수명 (예: hp, gold, inventory)")
    operation: Literal["add", "subtract", "set", "remove"] = Field(description="연산 방식")
    value: Any = Field(description="변경할 값 (숫자 또는 문자열)")


# 2. 선택지(Choice) 정의
class Choice(BaseModel):
    text: str = Field(description="선택지 텍스트")
    # [수정] AI가 가끔 연결할 씬 ID를 누락하는 경우(None)를 대비해 Optional로 변경
    next_scene_id: Optional[str] = Field(None, description="다음 씬 ID (비어있으면 현재 씬 유지)")
    effects: List[Effect] = Field(default=[], description="선택 시 발생할 효과 목록")


# 3. NPC 정의
class NPC(BaseModel):
    name: str = Field(description="NPC 이름")
    background: str = Field(description="배경 스토리 (비어있으면 AI 생성)")
    personality: str = Field(description="성격 (비어있으면 AI 생성)")
    hostility: Literal["Friendly", "Neutral", "Hostile"] = Field(description="적대 상태")
    description: str = Field(description="외양 묘사")


# 4. 씬(Scene) 정의
class Scene(BaseModel):
    scene_id: str
    title: str = Field(description="씬 제목")
    description: str = Field(description="씬 묘사 (상황, 분위기)")

    # 분기점 관련
    has_branch: bool = Field(description="분기점 유무")
    next_scene_ids: List[str] = Field(description="연결될 다음 씬 ID들 (분기면 2개 이상)")

    # 씬 종료/해결 조건
    required_key_item: Optional[str] = Field(None, description="이 씬을 끝내기 위해 필요한 아이템")
    required_action: Optional[str] = Field(None, description="이 씬을 끝내기 위해 필요한 행동 (예: 설득, 전투)")

    # 등장 요소
    npc_names: List[str] = Field(default=[], description="이 씬에 등장하는 NPC 이름들")

    # 선택지
    choices: List[Choice] = Field(default=[], description="플레이어가 선택할 수 있는 선택지 목록")


# 5. 엔딩(Ending) 정의
class Ending(BaseModel):
    ending_id: str
    title: str
    description: str = Field(description="엔딩 묘사")
    condition: str = Field(description="이 엔딩을 보기 위한 조건 (아이템, 행동, 스탯 등)")


# 6. 전체 시나리오
class GameScenario(BaseModel):
    title: str
    # 프롤로그
    prologue_text: str = Field(description="프롤로그 텍스트")
    global_rules: List[str] = Field(description="시나리오 전체에 적용되는 공통 룰")

    # 구성 요소
    npcs: List[NPC]
    scenes: List[Scene]
    endings: List[Ending]

    # 초기 상태
    initial_state: Dict[str, Any] = Field(description="플레이어 초기 스탯/아이템 (예: hp: int, inventory: list)")