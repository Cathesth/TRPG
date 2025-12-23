from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional

# --- Schema (Refined for Builder) ---

class Effect(BaseModel):
    target_var: str = Field(description="Variable name (e.g., hp, gold)")
    operation: str = Field(description="add, subtract, set")
    value: Any

class Choice(BaseModel):
    text: str = Field(description="Choice text shown to player")
    next_scene_id: Optional[str] = Field(None, description="Target Scene ID")
    effects: List[Effect] = Field(default=[])

class NPC(BaseModel):
    name: str
    role: str = Field(description="Role in the story")
    personality: str = Field(description="Personality traits")
    description: str = Field(description="Visual description")
    dialogue_style: str = Field(description="How they speak (e.g., rude, formal)")

class Scene(BaseModel):
    scene_id: str
    title: str
    description: str = Field(description="Detailed scene description. If input was empty, AI generates it.")
    required_item: Optional[str] = Field(None)
    required_action: Optional[str] = Field(None)
    npcs: List[str] = Field(default=[], description="Names of NPCs present")
    choices: List[Choice] = Field(default=[])

class Ending(BaseModel):
    ending_id: str
    title: str
    description: str
    condition: str

class GameScenario(BaseModel):
    title: str
    background_story: str
    prologue: str
    npcs: List[NPC]
    scenes: List[Scene]
    endings: List[Ending]