"""Pure version-pinned reducer. No model calls, dice generation, SQL, or I/O."""

from world_game.domain.common import GameError, digest
from world_game.domain.events import (
    AspectPayload,
    BeliefPayload,
    CheckPayload,
    CommitmentPayload,
    ConflictPayload,
    ConsequencePayload,
    ConversationPayload,
    CounterPayload,
    CreatedPayload,
    Event,
    EventBatch,
    KnowledgePayload,
    MovePayload,
    OfferPayload,
    PointsPayload,
    PortalPayload,
    PresentedPayload,
    PropositionPayload,
    QuestPayload,
    RelationshipPayload,
    RngPayload,
    ScenePayload,
    SchedulePayload,
    SessionPayload,
    StatusPayload,
    StressPayload,
    TimePayload,
    TriggerPayload,
    UtterancePayload,
)
from world_game.domain.state import Placement, WorldState
from world_game.engine.queries import co_located, container, grounding_holds, location_of, possesses

REDUCER_VERSION = "world-game-reducer-v1"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise GameError(message)


def apply_event(state: WorldState, event: Event) -> WorldState:
    s = state.model_copy(deep=True)
    p = event.payload
    if isinstance(p, MovePayload):
        require(
            p.entity_id in s.entities and p.to_id in s.entities,
            "Placement references are unavailable.",
        )
        e = s.entities[p.entity_id]
        require(
            container(s, e.id) == p.from_id and p.from_id != p.to_id,
            "Placement precondition changed.",
        )
        actor = event.actor_id
        if p.permission == "traverse":
            traversal = s.entities.get(p.portal_id or "")
            require(
                e.actor is not None and traversal is not None and traversal.portal is not None,
                "Traversal requires an actor and portal.",
            )
            assert traversal and traversal.portal
            require(
                traversal.portal.open
                and not traversal.portal.locked
                and set(traversal.portal.endpoints) == {p.from_id, p.to_id},
                "The passage is blocked.",
            )
            require(
                not any(
                    a.template_id == "jammed_portal"
                    and a.grounding.kind == "component_predicate"
                    and a.grounding.value_id == traversal.id
                    and grounding_holds(s, a)
                    for a in s.aspects.values()
                ),
                "The passage is obstructed.",
            )
        elif p.permission in ("take", "give", "offer"):
            require(e.item is not None, "Only items can be transferred.")
            require(actor is not None, "Transfer requires an actor.")
            assert actor
            if p.permission == "take":
                require(
                    p.to_id == actor and p.from_id == location_of(s, actor),
                    "Item is not available here.",
                )
            elif p.permission == "give":
                require(
                    p.from_id == actor and co_located(s, actor, p.to_id),
                    "Recipient must be here and the item possessed.",
                )
                recipient = s.entities[p.to_id]
                require(recipient.actor is not None, "Recipient must be a character.")
                accepted = (
                    (e.id == "item:records_key" and p.to_id == "actor:sen")
                    or (e.id == "item:archive_receipt" and p.to_id == "actor:oren")
                    or bool(recipient.actor is not None and recipient.actor.status == "taken_out")
                )
                require(accepted, "No authored recipient acceptance for this transfer.")
            else:
                require(
                    co_located(s, p.from_id, p.to_id)
                    and any(
                        o.status == "accepted"
                        and any(
                            t.item_id == e.id and t.from_id == p.from_id and t.to_id == p.to_id
                            for t in o.terms.transfers
                        )
                        for o in s.offers.values()
                    ),
                    "Offer does not authorize this transfer.",
                )
        elif p.permission == "retrieve":
            portal = s.entities[p.from_id].portal
            require(
                portal is not None and location_of(s, p.to_id) in portal.endpoints,
                "Lodged object is out of reach.",
            )
        elif p.permission == "lodge":
            portal = s.entities[p.to_id].portal
            require(
                portal is not None and location_of(s, p.from_id) in portal.endpoints,
                "Cannot lodge a remote object.",
            )
        elif p.permission == "scheduler":
            require(
                e.id == "item:ledger"
                and p.from_id == "location:records"
                and p.to_id == "container:archive",
                "Unknown scheduled transfer.",
            )
        elif p.permission == "confront":
            require(
                e.id == "actor:oren" and p.to_id == "location:courtyard",
                "Unknown confrontation move.",
            )
        elif p.permission == "concession":
            conceding_actor = s.entities["actor:lea"].actor
            require(
                s.conflict is not None
                and conceding_actor is not None
                and conceding_actor.status == "conceded"
                and (
                    (e.id == "actor:lea" and p.to_id == "location:gatehouse")
                    or (
                        e.id == "item:ledger"
                        and p.from_id == "actor:lea"
                        and p.to_id == location_of(s, "actor:lea")
                    )
                ),
                "Unapproved concession placement.",
            )
        elif p.permission == "conflict":
            require(
                s.conflict is not None
                and co_located(s, event.actor_id or "", p.from_id)
                and e.item is not None,
                "Conflict transfer is not available.",
            )
        e.placement = Placement(container_id=p.to_id)
    elif isinstance(p, PortalPayload):
        require(s.entities[p.portal_id].portal == p.before, "Portal precondition changed.")
        require(
            p.after.endpoints == p.before.endpoints
            and p.after.key_item_ids == p.before.key_item_ids,
            "Portal topology is immutable.",
        )
        require(not (p.after.open and p.after.locked), "A locked portal cannot be open.")
        s.entities[p.portal_id].portal = p.after.model_copy(deep=True)
    elif isinstance(p, PointsPayload):
        require(
            p.reason not in ("invoke", "refusal") or p.after == p.before - 1,
            "A paid invoke or refusal spends exactly one Fate point.",
        )
        require(
            p.reason != "compel" or p.after == p.before + 1,
            "An accepted compel awards exactly one Fate point.",
        )
        require(
            p.reason not in ("hostile_award", "concession") or p.after > p.before,
            "A Fate award must increase the balance.",
        )
        if p.account == "gm":
            require(s.scene.gm_fate_pool == p.before, "GM balance changed.")
            s.scene.gm_fate_pool = p.after
        else:
            sheet = s.entities[p.account].fate
            require(sheet is not None and sheet.fate_points == p.before, "Fate balance changed.")
            assert sheet
            require(
                p.reason != "refresh" or p.after == max(p.before, sheet.refresh),
                "Refresh cannot reduce points or exceed the refresh floor.",
            )
            sheet.fate_points = p.after
    elif isinstance(p, StressPayload):
        sheet = s.entities[p.actor_id].fate
        assert sheet
        field = p.track + "_stress_used"
        require(getattr(sheet, field) == p.before, "Stress precondition changed.")
        setattr(sheet, field, p.after)
    elif isinstance(p, ConsequencePayload):
        sheet = s.entities[p.actor_id].fate
        assert sheet
        require(getattr(sheet.consequence_slots, p.slot) == p.before, "Consequence slot changed.")
        setattr(sheet.consequence_slots, p.slot, p.after)
    elif isinstance(
        p,
        (
            AspectPayload,
            BeliefPayload,
            RelationshipPayload,
            ConversationPayload,
            OfferPayload,
            CommitmentPayload,
            QuestPayload,
            SchedulePayload,
        ),
    ):
        maps = {
            AspectPayload: "aspects",
            BeliefPayload: "beliefs",
            RelationshipPayload: "relationships",
            ConversationPayload: "conversations",
            OfferPayload: "offers",
            CommitmentPayload: "commitments",
            QuestPayload: "quests",
            SchedulePayload: "scheduled_effects",
        }
        records = getattr(s, maps[type(p)])
        require(records.get(p.id) == p.before, "Record precondition changed.")
        if p.after is None:
            records.pop(p.id, None)
        else:
            require(p.after.id == p.id, "Record identity cannot change.")
            records[p.id] = p.after.model_copy(deep=True)
    elif isinstance(p, KnowledgePayload):
        require(
            s.knowledge.get(p.holder_id) == p.before and p.after is not None,
            "Observation precondition changed.",
        )
        assert p.after
        s.knowledge[p.holder_id] = p.after.model_copy(deep=True)
    elif isinstance(p, (ScenePayload, SessionPayload, ConflictPayload)):
        field = (
            "scene"
            if isinstance(p, ScenePayload)
            else "session"
            if isinstance(p, SessionPayload)
            else "conflict"
        )
        require(getattr(s, field) == p.before, "Boundary precondition changed.")
        if field != "conflict":
            require(p.after is not None, "Boundary record cannot be removed.")
        setattr(s, field, p.after.model_copy(deep=True) if p.after else None)
    elif isinstance(p, TimePayload):
        require(
            s.clock == p.before
            and p.after.tick >= p.before.tick
            and p.after.player_turn >= p.before.player_turn,
            "Clock cannot move backward.",
        )
        s.clock = p.after.model_copy(deep=True)
    elif isinstance(p, StatusPayload):
        actor_component = s.entities[p.actor_id].actor
        assert actor_component
        require(actor_component.status == p.before, "Actor status changed.")
        actor_component.status = p.after
    elif isinstance(p, TriggerPayload):
        require(p.id not in s.applied_trigger_ids, "Trigger already applied.")
        s.applied_trigger_ids.append(p.id)
    elif isinstance(p, CounterPayload):
        require(s.next_id == p.before and p.after == p.before + 1, "Generated ID counter mismatch.")
        s.next_id = p.after
    elif isinstance(p, RngPayload):
        require(
            s.rng == p.before
            and p.after.seed == p.before.seed
            and p.after.counter >= p.before.counter,
            "Dice continuation mismatch.",
        )
        s.rng = p.after.model_copy(deep=True)
    elif isinstance(p, CreatedPayload):
        require(
            p.record.id == "item:archive_receipt"
            and p.record.id not in s.entities
            and p.record.item is not None
            and p.record.placement is not None
            and p.record.placement.container_id == "actor:lea",
            "Unsupported object creation.",
        )
        s.entities[p.record.id] = p.record.model_copy(deep=True)
    elif isinstance(p, PropositionPayload):
        old = s.propositions.get(p.record.id)
        require(old is None or old.truth == "unknown", "Established truth cannot be overwritten.")
        require(p.record.predicate == "claim", "Only bounded claim propositions may be generated.")
        s.propositions[p.record.id] = p.record.model_copy(deep=True)
    elif isinstance(p, PresentedPayload):
        require(
            possesses(s, p.actor_id, p.item_id)
            and all(co_located(s, p.actor_id, a) for a in p.audience_ids),
            "Presentation requires possession and a present audience.",
        )
    elif isinstance(p, UtterancePayload):
        require(
            p.speaker_id in s.entities
            and all(co_located(s, p.speaker_id, a) for a in p.audience_ids),
            "Audience cannot hear remote speech.",
        )
        require(
            all(ref in s.knowledge[p.speaker_id].known_record_ids for ref in p.disclosures),
            "Unauthorized disclosure.",
        )
        require(
            all(ref in s.propositions for ref in p.claims + p.disclosures),
            "Speech references unavailable claims.",
        )
    elif isinstance(p, CheckPayload):
        require(
            p.record.rng_continuation == s.rng,
            "Check must record already committed dice continuation.",
        )
    else:
        raise GameError("Unsupported event schema.")
    return s


def apply_staged_events(state: WorldState, events: list[Event]) -> WorldState:
    for event in events:
        state = apply_event(state, event)
    return state


def apply_batch(state: WorldState, batch: EventBatch) -> WorldState:
    from world_game.engine.validation import validate_batch

    issues = validate_batch(state, batch)
    if issues:
        raise GameError("; ".join(f"{i.path}: {i.message}" for i in issues))
    result = apply_staged_events(state, batch.events)
    result.world_version = batch.result_world_version
    require(digest(result) == batch.state_hash_after, "Batch state hash mismatch.")
    return result
