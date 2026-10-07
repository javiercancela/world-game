from typing import Literal, Protocol

from pydantic import BaseModel

from world_game.domain.common import GameError
from world_game.domain.proposals import (
    DecisionRequest,
    DecisionResult,
    ModelRequest,
    ModelResult,
    ModelRole,
)

ProviderName = Literal["scripted", "local", "openai"]


class ProviderError(GameError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class StructuredModel(Protocol):
    def cache_identity(self, role: ModelRole) -> str: ...

    def generate(
        self,
        *,
        role: ModelRole,
        request: ModelRequest,
        response_type: type[BaseModel],
        call_key: str,
        timeout_seconds: float | None = None,
    ) -> ModelResult: ...


class DecisionProvider(Protocol):
    def evaluate(self, request: DecisionRequest) -> DecisionResult: ...
