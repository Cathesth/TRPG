from pydantic import BaseModel, Field
from typing import List, Any, Optional


# --- Basic Components ---

class WorldSettings(BaseModel):
    """
    월드 규칙 설정 (Python으로만 처리)
    """
    hp_loss_per_move: int = Field(default=0, description="이동할 때마다 감소하는 HP (0이면 비활성화)")
    hp_zero_ending_id: Optional[str] = Field(None, description="HP가 0 이하가 되면 강제 이동할 엔딩 ID")
    sanity_loss_per_move: int = Field(default=0, description="이동할 때마다 감소하는 SANITY")
    sanity_zero_ending_id: Optional[str] = Field(None, description="SANITY가 0 이하가 되면 강제 이동할 엔딩 ID")
    auto_save_on_move: bool = Field(default=True, description="이동 시 자동 저장 여부")


class GlobalVariable(BaseModel):
    name: str = Field(description="Variable name (e.g., 'hp', 'gold', 'sanity')")
    initial_value: int = Field(default=0, description="Starting value")
    type: str = Field(default="int", description="int, boolean, string")


class Item(BaseModel):
    name: str = Field(description="Unique item name")
    description: str = Field(description="Item flavor text")
    is_key_item: bool = Field(default=False, description="If true, critical for progression")


# --- Logic Components ---

class Condition(BaseModel):
    target: str = Field(description="Variable name OR Item name")
    type: str = Field(description="'variable' or 'item'")
    operator: str = Field(description=">, <, ==, >=, <=, has, not_has")
    value: Any = Field(description="Comparison value (e.g., 50, true)")


class Effect(BaseModel):
    target: str = Field(description="Variable name OR Item name")
    type: str = Field(description="'variable' or 'item'")
    operation: str = Field(description="add, subtract, set, gain_item, lose_item")
    value: Any


# --- Scene Components (CHANGED) ---

class SceneTransition(BaseModel):
    """
    Choice(선택지) 대신 사용.
    플레이어가 특정 행동을 했을 때 다음 씬으로 넘어가는 '규칙'을 정의함.
    """
    target_scene_id: str = Field(description="ID of the destination scene")
    trigger: str = Field(
        description="The action or event that triggers this transition (e.g., 'Player opens the door', 'Player attacks the merchant'). NOT a UI button text.")
    conditions: List[Condition] = Field(default=[], description="Requirements for this transition to happen")
    effects: List[Effect] = Field(default=[], description="Side effects when this transition happens")


class NPC(BaseModel):
    name: str
    role: str = Field(description="Role in the story")
    personality: str = Field(description="Personality traits")
    description: str = Field(description="Visual description")
    image_prompt: Optional[str] = Field(None, description="Prompt for generating NPC portrait")
    dialogue_style: str = Field(description="How they speak")


class Scene(BaseModel):
    scene_id: str
    title: str
    description: str = Field(description="Detailed scene description text. Pure narrative.")
    image_prompt: Optional[str] = Field(None, description="Prompt for generating scene background image")

    # Legacy fields (Optional)
    required_item: Optional[str] = Field(None)
    required_action: Optional[str] = Field(None)

    npcs: List[str] = Field(default=[], description="Names of NPCs present in this scene")

    # Changed from choices to transitions
    transitions: List[SceneTransition] = Field(default=[],
                                               description="Possible paths to other scenes based on player actions.")


class Ending(BaseModel):
    ending_id: str
    title: str
    description: str
    image_prompt: Optional[str] = Field(None, description="Ending illustration prompt")
    condition: str = Field(description="Narrative condition")


# --- Root Schema ---

class GameScenario(BaseModel):
    title: str
    genre: str = Field(description="Fantasy, Sci-Fi, Horror, etc.")
    background_story: str
    prologue: str

    variables: List[GlobalVariable] = Field(default=[], description="Global state variables")
    items: List[Item] = Field(default=[], description="Registry of all items")

    npcs: List[NPC]
    scenes: List[Scene]
    endings: List[Ending]

    world_settings: WorldSettings = Field(default=WorldSettings(), description="Rules that govern the game world")
