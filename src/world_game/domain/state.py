from typing import Annotated, Literal

from pydantic import Field, model_validator

from world_game.domain.common import ID, Count, Text, Value
from world_game.domain.fate import Aspect, ConflictState, FateSheet, RngState, Source


class Identity(Value):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    aliases: list[str] = Field(default_factory=list, max_length=20)
    description: Text = ""


class Placement(Value):
    container_id: ID


class Location(Value):
    reachable: bool = True


class Portal(Value):
    endpoints: Annotated[list[ID], Field(min_length=2, max_length=2)]
    open: bool = False
    locked: bool = False
    key_item_ids: list[ID] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class Item(Value):
    tags: list[str] = Field(default_factory=list)
    quantity: Annotated[int, Field(ge=1)] = 1


class Actor(Value):
    profile_ref: ID
    goal_ids: list[ID]
    status: Literal["active", "conceded", "taken_out"] = "active"
    disposition: Annotated[int, Field(ge=-2, le=2)] = 0


class Entity(Value):
    id: ID
    kind: Literal["actor", "location", "portal", "item", "container"]
    identity: Identity
    placement: Placement | None = None
    location: Location | None = None
    portal: Portal | None = None
    item: Item | None = None
    actor: Actor | None = None
    fate: FateSheet | None = None

    @model_validator(mode="after")
    def components(self) -> "Entity":
        expected = {
            "actor": (True, False, False, False),
            "location": (False, True, False, False),
            "portal": (False, False, True, False),
            "item": (False, False, False, True),
            "container": (False, False, False, False),
        }[self.kind]
        if (
            self.actor is not None,
            self.location is not None,
            self.portal is not None,
            self.item is not None,
        ) != expected or (self.fate is not None) != (self.kind == "actor"):
            raise ValueError("entity kind and components disagree")
        if self.kind in ("actor", "item") and self.placement is None:
            raise ValueError("physical entity requires placement")
        if self.kind in ("location", "portal") and self.placement is not None:
            raise ValueError("locations and portals have no physical parent")
        return self


class FactArguments(Value):
    subject_id: ID
    object_id: ID | None = None
    value: str | int | bool | None = None


class Proposition(Value):
    id: ID
    predicate: Literal["expected", "incriminates", "fears", "claim"]
    arguments: FactArguments
    truth: Literal["true", "false", "unknown"]
    source: Source
    discovery_policy_id: ID


class Belief(Value):
    id: ID
    holder_id: ID
    proposition_id: ID
    stance: Literal["believes", "disbelieves", "uncertain"]
    source_event_ids: list[ID]
    acquired_tick: Count


class Observation(Value):
    record_id: ID
    field: Literal["presence", "placement", "portal", "speech", "presentation"]
    value: str | bool
    source_event_id: ID
    acquired_tick: Count


class KnowledgeState(Value):
    known_entity_ids: list[ID] = Field(default_factory=list)
    known_record_ids: list[ID] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)


class Relationship(Value):
    id: ID
    from_id: ID
    to_id: ID
    kind: Literal["trust"] = "trust"
    value: Annotated[int, Field(ge=-2, le=2)]
    source: Source


class Conversation(Value):
    id: ID
    participant_ids: list[ID]
    location_id: ID
    topic: Text
    recent_utterance_ids: list[ID] = Field(default_factory=list)
    unresolved_question: Text | None = None
    offer_ids: list[ID] = Field(default_factory=list)


class TransferTerm(Value):
    item_id: ID
    from_id: ID
    to_id: ID


class Fulfillment(Value):
    kind: Literal["returned", "delivered"]
    item_id: ID
    recipient_id: ID


class Commitment(Value):
    id: ID
    debtor_id: ID
    creditor_id: ID
    fulfillment_predicate: Fulfillment
    breach_trigger_id: ID
    status: Literal["active", "fulfilled", "broken"] = "active"
    source_event_id: ID


class OfferTerms(Value):
    template_id: Literal["collateral", "key_loan"]
    transfers: list[TransferTerm]
    commitment: Commitment
    portal_id: ID | None = None


class Offer(Value):
    id: ID
    proposer_id: ID
    recipient_id: ID
    terms: OfferTerms
    status: Literal["pending", "accepted", "declined", "expired"] = "pending"
    expires_tick: Count
    source_event_id: ID


class Quest(Value):
    id: ID
    definition_ref: ID
    stage: Literal["active", "acquired", "completed", "failed"]
    completed_objective_ids: list[ID] = Field(default_factory=list)
    terminal_result: Literal["success", "failure"] | None = None


class EffectArguments(Value):
    actor_id: ID | None = None
    item_id: ID | None = None
    portal_id: ID | None = None
    quest_id: ID = "recover_ledger"


class ScheduledEffect(Value):
    id: ID
    due_tick: Count
    priority: int = 0
    handler_id: Literal["verify_invitation", "ledger_deadline"]
    args: EffectArguments
    status: Literal["pending", "applied", "skipped", "canceled"] = "pending"


class Award(Value):
    subject_id: ID
    amount: Count


class SceneState(Value):
    id: ID
    definition_ref: ID
    sequence: Count
    participant_ids: list[ID]
    gm_fate_pool: Count
    deferred_awards: list[Award] = Field(default_factory=list)
    started_tick: Count


class SessionState(Value):
    id: ID
    sequence: Count
    completed_scene_ids: list[ID] = Field(default_factory=list)
    applied_boundary_ids: list[ID] = Field(default_factory=list)
    npc_carryover_credits: dict[ID, Count] = Field(default_factory=dict)
    finished: bool = False


class Clock(Value):
    player_turn: Count = 0
    tick: Count = 0


class WorldState(Value):
    schema_version: Literal[1] = 1
    campaign_id: ID
    world_version: Count = 0
    ruleset_ref: ID
    story_ref: ID
    clock: Clock = Field(default_factory=Clock)
    rng: RngState
    next_id: Annotated[int, Field(ge=1)] = 1
    entities: dict[ID, Entity]
    propositions: dict[ID, Proposition]
    beliefs: dict[ID, Belief] = Field(default_factory=dict)
    knowledge: dict[ID, KnowledgeState]
    relationships: dict[ID, Relationship]
    aspects: dict[ID, Aspect]
    conversations: dict[ID, Conversation] = Field(default_factory=dict)
    offers: dict[ID, Offer] = Field(default_factory=dict)
    commitments: dict[ID, Commitment] = Field(default_factory=dict)
    quests: dict[ID, Quest]
    scheduled_effects: dict[ID, ScheduledEffect]
    scene: SceneState
    session: SessionState
    conflict: ConflictState | None = None
    # v0.1 policy: memoize failed methods and single-use authored triggers.
    applied_trigger_ids: list[ID] = Field(default_factory=list)
