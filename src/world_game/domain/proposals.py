from typing import Literal

from pydantic import Field

from world_game.domain.common import ID, Text, Value
from world_game.engine.queries import ContextView

ModelRole = Literal["interpreter", "adjudicator", "npc", "narrator", "decision", "describer"]


class RulingProposal(Value):
    handler_id: str
    evidence_refs: list[ID]
    difficulty: Literal[0, 2, 4, 6]
    effect_template_ids: list[ID]
    explanation: Text


class NpcProposal(Value):
    selected_response: str
    utterance: Text
    claim_refs: list[ID]
    disclosure_refs: list[ID]
    offer_template_id: str | None


class NarrationResult(Value):
    prose: Text
    used_event_ids: list[ID]
    mentioned_entity_ids: list[ID]


class DescriptionResult(Value):
    prose: Text
    mentioned_entity_ids: list[ID]


class DecisionRequest(Value):
    question_id: ID
    rubric_version: str
    context_world_version: int
    perspective_actor_id: ID
    context_hash: str
    evidence_refs: list[ID]
    kind: Literal["choice", "score", "yes_no"]
    question: Text
    options_or_rubric: list[Text]
    context: ContextView


class DecisionResult(Value):
    value: str | int | bool
    probabilities: list[float] | None = None
    confidence: float | None = None
    provider: str
    model_version: str
    request_id: str | None = None


class RoleProfile(Value):
    actor_id: ID
    public: Text
    private: Text
    goals: list[ID]


class ModelRequest(Value):
    context: ContextView
    text: Text
    permitted_responses: list[str] = Field(default_factory=list)
    evidence_refs: list[ID] = Field(default_factory=list)
    fallback: Text = ""
    repair: Text | None = None
    role_profile: RoleProfile | None = None


class Usage(Value):
    input_tokens: int = 0
    output_tokens: int = 0


class ModelResult(Value):
    payload_json: str
    provider: str
    model: str
    provider_request_id: str | None = None
    latency_ms: int = 0
    usage: Usage = Field(default_factory=Usage)
    prompt_version: str = "roles-v1"
    context_hash: str = ""
    prompt_hash: str = ""
    role: ModelRole | None = None
    request_json: str = ""
    status: Literal["ok", "failed"] = "ok"
