from world_game.domain.common import digest
from world_game.domain.events import CounterPayload, Event, EventBatch, Payload
from world_game.domain.intents import PendingResolution
from world_game.domain.state import WorldState
from world_game.engine.reducer import apply_event, apply_staged_events


class EventBuilder:
    def __init__(self, state: WorldState, pending: PendingResolution) -> None:
        self.base = state
        self.pending = pending
        self.state = apply_staged_events(state, pending.staged_events)
        self.rule_id = (
            pending.resolution_plan.rule_id if pending.resolution_plan else "authored.boundary"
        )
        self.actor_id: str | None = pending.actor_id

    def emit(self, payload: Payload, observed_by: list[str] | None = None) -> Event:
        index = len(self.pending.staged_events)
        event = Event(
            id=f"event:{self.pending.input_id}:{index}",
            sequence=index,
            type=payload.kind,
            payload=payload,
            actor_id=self.actor_id,
            rule_id=self.rule_id,
            cause_ids=[self.pending.staged_events[-1].id] if index else [],
            observed_by=sorted(set(observed_by or [])),
            tick=self.state.clock.tick,
        )
        self.state = apply_event(self.state, event)
        self.pending.staged_events.append(event)
        return event

    def allocate(self, kind: str) -> str:
        counter = self.state.next_id
        id = f"{kind}:{self.state.campaign_id}:{counter}"
        self.emit(CounterPayload(before=counter, after=counter + 1))
        return id

    def batch(self) -> EventBatch:
        state = self.state.model_copy(update={"world_version": self.base.world_version + 1})
        return EventBatch(
            batch_id=f"batch:{self.pending.input_id}",
            input_id=self.pending.input_id,
            base_world_version=self.base.world_version,
            result_world_version=state.world_version,
            player_turn_after=state.clock.player_turn,
            events=self.pending.staged_events,
            state_hash_after=digest(state),
        )
