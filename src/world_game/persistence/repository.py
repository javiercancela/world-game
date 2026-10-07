from typing import Protocol

from world_game.domain.events import EventBatch
from world_game.domain.intents import PendingResolution
from world_game.domain.state import WorldState


class Repository(Protocol):
    def load(self) -> WorldState: ...
    def pending(self) -> PendingResolution | None: ...
    def save_pending(
        self,
        pending: PendingResolution,
        expected_revision: int | None = None,
        response_id: str | None = None,
        option_id: str | None = None,
    ) -> None: ...
    def commit(
        self, batch: EventBatch, pending: PendingResolution, fallback: str
    ) -> WorldState: ...
