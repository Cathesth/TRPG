from pydantic import BaseModel, Field
from typing import List, Any, Optional, Dict


# --- Basic Components ---

class GlobalVariable(BaseModel):
    name: str = Field(description="Variable name (e.g., 'hp', 'gold', 'sanity')")
    initial_value: int = Field(default=0, description="Starting value")
    type: str = Field(default="int", description="int, boolean, string")


# --- Logic Components (Effect를 Item보다 먼저 정의) ---

class Effect(BaseModel):
    target: str = Field(description="Variable name OR Item name")
    type: str = Field(description="'variable' or 'item'")
    operation: str = Field(description="add, subtract, set, gain_item, lose_item")
    value: Any


class Condition(BaseModel):
    target: str = Field(description="Variable name OR Item name")
    type: str = Field(description="'variable' or 'item'")
    operator: str = Field(description=">, <, ==, >=, <=, has, not_has")
    value: Any = Field(description="Comparison value (e.g., 50, true)")


# --- Item Definition (Effect 참조 가능) ---

class Item(BaseModel):
    name: str = Field(description="Unique item name")
    description: str = Field(description="Item flavor text")
    is_key_item: bool = Field(default=False, description="If true, critical for progression")
    # ✅ 아이템 사용 시 발동할 효과 정의
    effects: List[Effect] = Field(default=[], description="Effects triggered when item is used")
    # ✅ 아이템 사용 가능 여부
    usable: bool = Field(default=True, description="Whether this item can be used by the player")


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

    world_state: Optional[Dict[str, Any]] = Field(default=None, description="The state of the world, affecting all scenes and characters")


# --- Game Action Schema (for API endpoints) ---

class GameAction(BaseModel):
    action: str = Field(default='', description="Player action text")
    model: str = Field(default='openai/tngtech/deepseek-r1t2-chimera:free', description="AI model to use")
    provider: str = Field(default='deepseek', description="AI provider")
    session_id: Optional[str] = Field(None, description="Session ID for continuing game")
    session_key: Optional[str] = Field(None, description="Session key for DB persistence")
