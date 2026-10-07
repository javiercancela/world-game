"""Finite scripted decision boundary; a later calibrated Jev adapter can implement it."""

from world_game.domain.common import digest
from world_game.domain.proposals import DecisionRequest, DecisionResult
from world_game.models.interfaces import ProviderError


class ScriptedDecisionProvider:
    def __init__(self, answers: dict[str, str | int | bool]) -> None:
        self.answers = answers
        self.requests: list[DecisionRequest] = []

    def evaluate(self, request: DecisionRequest) -> DecisionResult:
        if request.context_hash != digest(request.context):
            raise ProviderError("Decision context hash mismatch.")
        if request.perspective_actor_id != request.context.viewer_id:
            raise ProviderError("Decision perspective mismatch.")
        value = self.answers.get(request.question_id)
        if value is None:
            raise ProviderError("No scripted answer for this decision.")
        if (
            request.kind == "choice"
            and (type(value) is not str or value not in request.options_or_rubric)
            or request.kind == "score"
            and type(value) is not int
            or request.kind == "yes_no"
            and type(value) is not bool
        ):
            raise ProviderError("Scripted answer does not satisfy the finite rubric.")
        self.requests.append(request)
        return DecisionResult(value=value, provider="scripted", model_version="decision-v1")
