"""Only typed, engine-authored events may change canonical state."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from world_game.domain.common import ID, Count, Text, Value
from world_game.domain.fate import Aspect, CheckRecord, ConflictState, RngState
from world_game.domain.state import (
    Belief,
    Clock,
    Commitment,
    Conversation,
    Entity,
    KnowledgeState,
    Offer,
    Portal,
    Proposition,
    Quest,
    Relationship,
    SceneState,
    ScheduledEffect,
    SessionState,
)


class Change[T: Value](Value):
    before: T | None
    after: T | None


class MovePayload(Value):
    kind: Literal["EntityMoved", "ItemTransferred"]
    entity_id: ID
    from_id: ID
    to_id: ID
    permission: Literal[
        "traverse",
        "take",
        "give",
        "offer",
        "scheduler",
        "confront",
        "concession",
        "retrieve",
        "lodge",
        "conflict",
    ]
    portal_id: ID | None = None


class PortalPayload(Value):
    kind: Literal["PortalChanged"] = "PortalChanged"
    portal_id: ID
    before: Portal
    after: Portal


class UtterancePayload(Value):
    kind: Literal["UtteranceMade"] = "UtteranceMade"
    speaker_id: ID
    text: Text
    audience_ids: list[ID]
    claims: list[ID] = Field(default_factory=list)
    disclosures: list[ID] = Field(default_factory=list)


class PresentedPayload(Value):
    kind: Literal["ItemPresented"] = "ItemPresented"
    actor_id: ID
    item_id: ID
    audience_ids: list[ID]


class KnowledgePayload(Change[KnowledgeState]):
    kind: Literal["ObservationRecorded"] = "ObservationRecorded"
    holder_id: ID


class BeliefPayload(Change[Belief]):
    kind: Literal["BeliefChanged"] = "BeliefChanged"
    id: ID


class RelationshipPayload(Change[Relationship]):
    kind: Literal["RelationshipChanged"] = "RelationshipChanged"
    id: ID


class ConversationPayload(Change[Conversation]):
    kind: Literal["ConversationChanged"] = "ConversationChanged"
    id: ID


class OfferPayload(Change[Offer]):
    kind: Literal["OfferMade", "OfferResolved"]
    id: ID


class CommitmentPayload(Change[Commitment]):
    kind: Literal["CommitmentMade", "CommitmentResolved"]
    id: ID


class AspectPayload(Change[Aspect]):
    kind: Literal["AspectCreated", "AspectChanged", "AspectRemoved"]
    id: ID
    reason: Text = ""


class PointsPayload(Value):
    kind: Literal["FatePointsChanged"] = "FatePointsChanged"
    account: ID
    before: Count
    after: Count
    reason: Literal["invoke", "hostile_award", "compel", "refusal", "concession", "refresh"]


class StressPayload(Value):
    kind: Literal["StressChanged"] = "StressChanged"
    actor_id: ID
    track: Literal["physical", "mental"]
    before: Count
    after: Count


class ConsequencePayload(Value):
    kind: Literal["ConsequenceChanged"] = "ConsequenceChanged"
    actor_id: ID
    slot: Literal["mild", "moderate", "severe"]
    before: ID | None
    after: ID | None


class CheckPayload(Value):
    kind: Literal["CheckResolved"] = "CheckResolved"
    record: CheckRecord


class QuestPayload(Change[Quest]):
    kind: Literal["QuestTransitioned"] = "QuestTransitioned"
    id: ID


class CreatedPayload(Value):
    kind: Literal["EntityCreated"] = "EntityCreated"
    template_id: Literal["archive_receipt"] = "archive_receipt"
    record: Entity


class PropositionPayload(Value):
    kind: Literal["PropositionEstablished"] = "PropositionEstablished"
    record: Proposition


class SchedulePayload(Change[ScheduledEffect]):
    kind: Literal["ScheduledEffectAdded", "ScheduledEffectResolved"]
    id: ID


class TimePayload(Value):
    kind: Literal["TimeAdvanced"] = "TimeAdvanced"
    before: Clock
    after: Clock


class ScenePayload(Change[SceneState]):
    kind: Literal["SceneEnded", "SceneStarted", "SceneChanged"]
    transition_id: ID


class SessionPayload(Change[SessionState]):
    kind: Literal["SessionStarted", "SessionChanged"]
    transition_id: ID


class ConflictPayload(Change[ConflictState]):
    kind: Literal["ConflictStarted", "ConflictAdvanced", "ConflictEnded"]
    transition_id: ID


class StatusPayload(Value):
    kind: Literal["ActorStatusChanged"] = "ActorStatusChanged"
    actor_id: ID
    before: Literal["active", "conceded", "taken_out"]
    after: Literal["active", "conceded", "taken_out"]


class TriggerPayload(Value):
    kind: Literal["TriggerApplied", "BreakthroughReached"]
    id: ID


class CounterPayload(Value):
    kind: Literal["IdAllocated"] = "IdAllocated"
    before: Count
    after: Count


class RngPayload(Value):
    kind: Literal["RngAdvanced"] = "RngAdvanced"
    before: RngState
    after: RngState


Payload = Annotated[
    MovePayload
    | PortalPayload
    | UtterancePayload
    | PresentedPayload
    | KnowledgePayload
    | BeliefPayload
    | RelationshipPayload
    | ConversationPayload
    | OfferPayload
    | CommitmentPayload
    | AspectPayload
    | PointsPayload
    | StressPayload
    | ConsequencePayload
    | CheckPayload
    | QuestPayload
    | CreatedPayload
    | PropositionPayload
    | SchedulePayload
    | TimePayload
    | ScenePayload
    | SessionPayload
    | ConflictPayload
    | StatusPayload
    | TriggerPayload
    | CounterPayload
    | RngPayload,
    Field(discriminator="kind"),
]


class Event(Value):
    id: ID
    sequence: Count
    type: str
    schema_version: Literal[1] = 1
    payload: Payload
    actor_id: ID | None
    rule_id: ID
    cause_ids: list[ID]
    observed_by: list[ID]
    tick: Count

    @model_validator(mode="after")
    def matching_type(self) -> "Event":
        if self.type != self.payload.kind:
            raise ValueError("event type disagrees with payload")
        return self


class EventBatch(Value):
    batch_id: ID
    input_id: ID
    base_world_version: Count
    result_world_version: Count
    player_turn_after: Count
    events: list[Event] = Field(min_length=1, max_length=100)
    state_hash_after: str
    schema_version: Literal[1] = 1
