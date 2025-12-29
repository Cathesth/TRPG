from pydantic import BaseModel, Field
from typing import List, Any, Optional, Union


# --- Basic Components ---

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


# --- Scene Components ---

class Choice(BaseModel):
    text: str = Field(description="Choice text shown to player")
    next_scene_id: Optional[str] = Field(None, description="Target Scene ID")
    conditions: List[Condition] = Field(default=[], description="Requirements to see/choose this option")
    effects: List[Effect] = Field(default=[], description="Immediate effects upon choosing")


class NPC(BaseModel):
    name: str
    role: str = Field(description="Role in the story (e.g., Merchant, Villain)")
    personality: str = Field(description="Personality traits")
    description: str = Field(description="Visual description for player")
    image_prompt: Optional[str] = Field(None, description="Prompt for generating NPC portrait")
    dialogue_style: str = Field(description="How they speak (e.g., rude, formal)")


class Scene(BaseModel):
    scene_id: str
    title: str
    description: str = Field(description="Detailed scene description text")
    image_prompt: Optional[str] = Field(None, description="Prompt for generating scene background image")

    # Simple requirements (Legacy support, prefer using Choice conditions or entry conditions)
    required_item: Optional[str] = Field(None)
    required_action: Optional[str] = Field(None)

    npcs: List[str] = Field(default=[], description="Names of NPCs present in this scene")
    choices: List[Choice] = Field(default=[])


class Ending(BaseModel):
    ending_id: str
    title: str
    description: str
    image_prompt: Optional[str] = Field(None, description="Ending illustration prompt")
    condition: str = Field(description="Narrative condition (e.g., 'If player has the Holy Grail')")


# --- Root Schema ---

class GameScenario(BaseModel):
    title: str
    genre: str = Field(description="Fantasy, Sci-Fi, Horror, etc.")
    background_story: str
    prologue: str

    # State Definitions
    variables: List[GlobalVariable] = Field(default=[], description="Global state variables (HP, Sanity, etc.)")
    items: List[Item] = Field(default=[], description="Registry of all items in the game")

    # Content
    npcs: List[NPC]
    scenes: List[Scene]
    endings: List[Ending]