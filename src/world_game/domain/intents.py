from typing import Annotated, Literal

from pydantic import Field

from world_game.domain.common import ID, Count, Text, Value
from world_game.domain.fate import Dice, Draw, InvokeUse, ResolutionPlan, RngState

Phase = Literal[
    "interpreting",
    "awaiting_clarification",
    "planning",
    "awaiting_pre_roll_choice",
    "rolling",
    "awaiting_invoke",
    "resolving_outcome",
    "awaiting_outcome_choice",
    "staging_reactions",
    "validating",
    "committed",
    "presenting",
    "failed",
]
ChoiceKind = Literal[
    "clarification",
    "invoke",
    "cost",
    "harm",
    "concession",
    "compel",
    "next_actor",
    "offer",
    "pre_roll",
]

Handler = Literal[
    "speak",
    "social_overcome",
    "discover_advantage",
    "create_advantage",
    "move",
    "present_item",
    "take_item",
    "transfer_item",
    "retrieve_lodged_item",
    "use_key",
    "pick_lock",
    "attack",
    "treat_consequence",
    "wait",
    "accept_offer",
]
HANDLERS: tuple[str, ...] = (
    "speak",
    "social_overcome",
    "discover_advantage",
    "create_advantage",
    "move",
    "present_item",
    "take_item",
    "transfer_item",
    "retrieve_lodged_item",
    "use_key",
    "pick_lock",
    "attack",
    "treat_consequence",
    "wait",
    "accept_offer",
)


class Intent(Value):
    kind: Literal["action"] = "action"
    handler_id: Handler
    target_ids: list[ID] = Field(default_factory=list, max_length=3)
    goal: Text = ""
    method: Text = ""
    speech: Text = ""
    adjacent_move_id: ID | None = None
    new_claim: Text | None = None
    presented_item_ids: list[ID] = Field(default_factory=list, max_length=3)
    claim_refs: list[ID] = Field(default_factory=list, max_length=4)
    unresolved_references: list[Text] = Field(default_factory=list, max_length=3)


class Clarification(Value):
    kind: Literal["clarification"] = "clarification"
    prompt: Text
    candidates: list[ID] = Field(default_factory=list, max_length=10)


class IntentResult(Value):
    payload: Annotated[Intent | Clarification, Field(discriminator="kind")]


class ChoiceCommand(Value):
    kind: Literal[
        "pass",
        "invoke",
        "accept",
        "decline",
        "refuse",
        "object",
        "absorb",
        "taken_out",
        "trade",
        "keep",
        "continue",
        "concede",
        "next_actor",
        "clarify",
        "major_cost",
        "failure",
    ]
    aspect_id: ID | None = None
    mode: Literal["bonus", "reroll"] = "bonus"
    payment: Literal["free", "paid"] = "paid"
    stress: Count = 0
    slots: list[Literal["mild", "moderate", "severe"]] = Field(default_factory=list)
    target_id: ID | None = None
    terms: Literal["custody", "abandon"] | None = None


class ChoiceOption(Value):
    id: ID
    label: Text
    command: ChoiceCommand


class Choice(Value):
    choice_id: ID
    kind: ChoiceKind
    prompt: Text
    options: list[ChoiceOption] = Field(max_length=100)
    context_hash: str


class PendingResolution(Value):
    interaction_id: ID
    input_id: ID
    base_world_version: Count
    choice_revision: Count = 0
    phase: Phase
    actor_id: ID
    raw_input: Text
    interpreted_intent: Intent | None = None
    resolution_plan: ResolutionPlan | None = None
    # Typed events are imported below to avoid an event/intents import cycle.
    staged_events: list["Event"] = Field(default_factory=list, max_length=100)
    staged_rng: RngState
    recorded_rolls: list[Draw] = Field(default_factory=list, max_length=100)
    actor_dice: Dice | None = None
    defender_dice: Dice | None = None
    actor_bonus: int = 0
    defender_bonus: int = 0
    invoke_ledger: list[InvokeUse] = Field(default_factory=list, max_length=100)
    provisional_resource_balances: dict[ID, Count] = Field(default_factory=dict)
    completed_model_call_keys: list[ID] = Field(default_factory=list)
    approved_npc_event_ids: list[ID] = Field(default_factory=list)
    provider_attempts: Count = 0
    provider_elapsed_ms: Count = 0
    current_choice: Choice | None = None
    completed_choices: list[ID] = Field(default_factory=list)
    resume_phase: Phase | None = None
    pass_count: Count = 0
    damage: Count = 0
    defender_id: ID | None = None
    output_lines: list[Text] = Field(default_factory=list)
    outcome_selection: str | None = None
    outcome_applied: bool = False
    effects_applied: bool = False
    time_applied: bool = False
    conflict_order_applied: bool = False
    check_staged: bool = False
    next_actor_candidates: list[ID] = Field(default_factory=list)


from world_game.domain.events import Event  # noqa: E402

PendingResolution.model_rebuild()
