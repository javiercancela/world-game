from typing import Annotated, Literal

from pydantic import Field

from world_game.domain.common import ID, Count, Text, Value

Skill = Literal[
    "athletics",
    "fight",
    "notice",
    "investigate",
    "influence",
    "deceive",
    "stealth",
    "craft",
    "will",
]
SKILLS: tuple[Skill, ...] = (
    "athletics",
    "fight",
    "notice",
    "investigate",
    "influence",
    "deceive",
    "stealth",
    "craft",
    "will",
)
Action = Literal["overcome", "create_advantage", "attack", "defend"]
Outcome = Literal["failure", "tie", "success", "style"]
Harm = Literal["physical", "mental"]
Slot = Literal["mild", "moderate", "severe"]
Severity = Literal[2, 4, 6]
Die = Annotated[int, Field(ge=-1, le=1)]
Dice = Annotated[list[Die], Field(min_length=4, max_length=4)]


class ConsequenceSlots(Value):
    mild: ID | None = None
    moderate: ID | None = None
    severe: ID | None = None


class FateSheet(Value):
    skills: dict[Skill, Annotated[int, Field(ge=0, le=4)]]
    stunt_ids: list[ID] = Field(default_factory=list, max_length=3)
    refresh: Count = 0
    fate_points: Count = 0
    physical_stress_used: Count = 0
    mental_stress_used: Count = 0
    consequence_slots: ConsequenceSlots = Field(default_factory=ConsequenceSlots)
    stress_capacity_override: Annotated[int, Field(ge=0, le=6)] | None = None
    has_consequences: bool = True


class Scope(Value):
    kind: Literal["character", "location", "conversation", "scene"]
    id: ID


class TraitGrounding(Value):
    kind: Literal["authored_trait"] = "authored_trait"
    id: ID


class ComponentGrounding(Value):
    kind: Literal["component_predicate"] = "component_predicate"
    id: ID
    predicate: Literal["possessed", "lodged", "burning", "noisy", "open"]
    value_id: ID | None = None


class RecordGrounding(Value):
    kind: Literal["proposition", "relationship", "belief"]
    id: ID


Grounding = Annotated[
    TraitGrounding | ComponentGrounding | RecordGrounding, Field(discriminator="kind")
]


class Lifetime(Value):
    kind: Literal["persistent", "scene_bound", "next_eligible_use", "until_grounding_false"]


class Source(Value):
    kind: Literal["story", "event"]
    id: ID


class Recovery(Value):
    severity: Literal[2, 4, 6]
    harm_type: Harm
    slot: Slot = "mild"
    treated: bool = False
    treatment_scene: Count | None = None
    treatment_session: Count | None = None


AspectTemplate = Literal[
    "momentary_opening",
    "noisy_intrusion",
    "off_balance",
    "focused",
    "cover",
    "jammed_portal",
    "burning",
]


class Aspect(Value):
    id: ID
    text: Text
    kind: Literal["character", "situation", "boost", "consequence"]
    subject_id: ID
    scope: Scope
    grounding: Grounding
    known_by: list[ID]
    free_invokes: dict[ID, Count] = Field(default_factory=dict)
    lifetime: Lifetime
    source: Source
    relevance_tags: list[str] = Field(default_factory=list)
    recovery: Recovery | None = None
    template_id: AspectTemplate | None = None


class RngState(Value):
    algorithm: Literal["sha256-counter-v1"] = "sha256-counter-v1"
    seed: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    counter: Annotated[int, Field(ge=0, le=2**64)] = 0


class Draw(Value):
    check_id: ID
    side: Literal["actor", "defender"]
    reroll_index: Count
    faces: Literal[3] = 3
    seed_ref: str
    before_counter: Count
    after_counter: Count
    dice: Dice


class Opposition(Value):
    kind: Literal["static", "active"]
    difficulty: Annotated[int, Field(ge=0, le=8)] = 0
    defender_id: ID | None = None
    skill: Skill = "will"


class ResolutionPlan(Value):
    rule_id: ID
    handler_id: ID
    actor_id: ID
    target_id: ID | None = None
    action: Action | None = None
    skill: Skill | None = None
    opposition: Opposition | None = None
    eligible_stunts: list[ID] = Field(default_factory=list)
    stunt_bonus: int = 0
    time_cost: Annotated[int, Field(ge=0, le=1)] = 1
    goal: Text
    method: Text
    allowed_consequences: list[ID] = Field(default_factory=list)
    advantage_kind: Literal["new", "known", "unknown"] = "new"
    aspect_id: ID | None = None
    harm_type: Harm = "physical"
    stakes: Text = ""
    failure_template_id: ID = "no_effect"
    minor_cost_template_id: ID = "no_effect"
    major_cost_template_id: ID | None = None


class InvokeUse(Value):
    aspect_id: ID
    controller_id: ID
    side: Literal["actor", "defender"]
    payment: Literal["free", "paid"]
    mode: Literal["bonus", "reroll"]


class CheckRecord(Value):
    id: ID
    plan: ResolutionPlan
    actor_dice: Dice
    defender_dice: Dice | None = None
    actor_bonus: int = 0
    defender_bonus: int = 0
    invoked_aspect_uses: list[InvokeUse] = Field(default_factory=list)
    margin: int
    result: Outcome
    rng_continuation: RngState
    draws: list[Draw]


class ConflictParticipant(Value):
    actor_id: ID
    side_id: ID


class ConflictState(Value):
    id: ID
    scene_id: ID
    participants: list[ConflictParticipant]
    active_actor_id: ID
    exchange: Count = 1
    acted_ids: list[ID] = Field(default_factory=list)
    consequences_taken: dict[ID, Count] = Field(default_factory=dict)
    pending_concession_awards: dict[ID, Count] = Field(default_factory=dict)
    last_charged_exchange: Count = 0
    harm_type: Harm = "physical"
