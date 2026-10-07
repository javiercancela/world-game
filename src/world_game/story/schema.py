from typing import Annotated, Literal

from pydantic import Field

from world_game.domain.common import ID, Text, Value
from world_game.domain.fate import Action, Skill
from world_game.domain.state import WorldState


class Manifest(Value):
    id: Literal["gate-at-dusk"]
    version: Literal["1.0"] = "1.0"
    schema_version: Literal[1] = 1
    ruleset_id: Literal["fate-condensed-cli-v1"]
    entry_scene_id: ID
    player_id: Literal["actor:lea"]
    resources: list[str]
    engine_version: Literal["0.1"] = "0.1"
    reducer_version: Literal["world-game-reducer-v1"] = "world-game-reducer-v1"


class AtomicPredicate(Value):
    kind: Literal[
        "co_located",
        "possesses",
        "portal_state",
        "has_known_belief",
        "aspect_active",
        "quest_stage",
        "conversation_participant",
    ]
    subject_id: ID
    object_id: ID | None = None
    value: str | bool | None = None
    portal_field: Literal["open", "locked"] = "open"


class BooleanPredicate(Value):
    kind: Literal["all", "any", "not"]
    children: list["Predicate"] = Field(min_length=1, max_length=8)


Predicate = Annotated[AtomicPredicate | BooleanPredicate, Field(discriminator="kind")]
BooleanPredicate.model_rebuild()


class CheckDefinition(Value):
    kind: Literal["none", "static", "active"]
    action: Action | None = None
    skill_id: Skill | None = None
    difficulty: Annotated[int, Field(ge=0, le=6)] = 0
    defense_skill: Skill = "will"
    goal_tag: str


class Outcomes(Value):
    failure: ID
    minor_cost: ID
    success: ID
    style: ID
    optional_major_cost: ID | None = None


class Rule(Value):
    id: ID
    classification: Literal["Fate rule", "v0.1 policy", "authored content"]
    handler_id: str
    intent_match_tags: list[str]
    argument_schema_id: ID
    preconditions: list[Predicate]
    check: CheckDefinition
    cost_ticks: Literal[0, 1] = 1
    permitted_stunt_ids: list[ID]
    outcomes: Outcomes
    visibility_policy_id: ID
    reaction_policy_id: ID


class Stunt(Value):
    id: Literal["official_bearing", "read_the_room", "quick_hands"]
    classification: Literal["authored content"] = "authored content"
    action: Action
    skill_id: Skill
    handler_id: str
    goal: str
    item_tag: str | None = None
    requires_conversation: bool = False


class Rules(Value):
    id: Literal["fate-condensed-cli-v1"]
    stunts: list[Stunt]
    actions: list[Rule]
    aspect_templates: list[str]
    consequence_catalog: dict[str, list[str]]


class Profile(Value):
    id: ID
    actor_id: ID
    public: Text
    private: Text
    private_known_by: list[ID]


class QuestDefinition(Value):
    id: ID
    stages: list[str]
    terminal_stages: list[str]


class SceneDefinition(Value):
    id: ID
    location_ids: list[ID]
    stakes: Text


class Dialogue(Value):
    version: str
    lines: dict[str, Text]


class StoryPackage(Value):
    manifest: Manifest
    initial_state: WorldState
    profiles: list[Profile]
    rules: Rules
    quests: list[QuestDefinition]
    scenes: list[SceneDefinition]
    dialogue: Dialogue
    # Exact resource text is persisted for portability, independent of installed files.
    resources: dict[str, str]
