"""Authored boundaries, never application-start or player-requested resets."""

from world_game.domain.events import (
    AspectPayload,
    ConsequencePayload,
    KnowledgePayload,
    PointsPayload,
    ScenePayload,
    SessionPayload,
    StressPayload,
    TriggerPayload,
)
from world_game.domain.state import Observation, SceneState
from world_game.engine.actions import PC
from world_game.engine.builder import EventBuilder
from world_game.engine.queries import container, grounding_holds, location_of, visible_entity_ids


def observe(builder: EventBuilder) -> None:
    for actor_id in sorted(builder.state.knowledge):
        old = builder.state.knowledge[actor_id]
        new = old.model_copy(deep=True)
        visible = visible_entity_ids(builder.state, actor_id)
        new.known_entity_ids = sorted(set(new.known_entity_ids + visible))
        for id in visible:
            e = builder.state.entities[id]
            value = container(builder.state, id) or (str(e.portal.open) if e.portal else id)
            if not any(
                o.record_id == id
                and o.value == value
                and o.field == ("portal" if e.portal else "presence")
                and o.acquired_tick == builder.state.clock.tick
                for o in new.observations
            ):
                new.observations.append(
                    Observation(
                        record_id=id,
                        field="portal" if e.portal else "presence",
                        value=value,
                        source_event_id=f"event:{builder.pending.input_id}:{len(builder.pending.staged_events)}",
                        acquired_tick=builder.state.clock.tick,
                    )
                )
        # Actual approved disclosures, unlike unsupported narration, grant knowledge.
        from world_game.domain.events import UtterancePayload

        for event in builder.pending.staged_events:
            if (
                isinstance(event.payload, UtterancePayload)
                and actor_id in event.payload.audience_ids
            ):
                new.known_record_ids = sorted(set(new.known_record_ids + event.payload.disclosures))
                if not any(o.record_id == event.id for o in new.observations):
                    new.observations.append(
                        Observation(
                            record_id=event.id,
                            field="speech",
                            value=event.payload.text,
                            source_event_id=event.id,
                            acquired_tick=event.tick,
                        )
                    )
        if new != old:
            builder.emit(KnowledgePayload(holder_id=actor_id, before=old, after=new), [actor_id])


def expire_grounding(builder: EventBuilder) -> None:
    for a in list(builder.state.aspects.values()):
        if not grounding_holds(builder.state, a):
            builder.emit(
                AspectPayload(
                    kind="AspectRemoved",
                    id=a.id,
                    before=a,
                    after=None,
                    reason="grounding no longer holds",
                ),
                a.known_by,
            )


def recover_consequences(
    builder: EventBuilder,
    closing_scene: bool = False,
    closing_session: bool = False,
    breakthrough: bool = False,
) -> None:
    for a in list(builder.state.aspects.values()):
        r = a.recovery
        if not r or not r.treated:
            continue
        eligible = (
            (
                r.severity == 2
                and closing_scene
                and r.treatment_scene is not None
                and builder.state.scene.sequence > r.treatment_scene
            )
            or (
                r.severity == 4
                and closing_session
                and r.treatment_session is not None
                and builder.state.session.sequence > r.treatment_session
            )
            or (r.severity == 6 and breakthrough)
        )
        if not eligible:
            continue
        sheet = builder.state.entities[a.subject_id].fate
        assert sheet
        for slot in ("mild", "moderate", "severe"):
            ref = getattr(sheet.consequence_slots, slot)
            if ref == a.id:
                builder.emit(
                    ConsequencePayload(actor_id=a.subject_id, slot=slot, before=ref, after=None)
                )
        builder.emit(
            AspectPayload(
                kind="AspectRemoved",
                id=a.id,
                before=a,
                after=None,
                reason="authored recovery boundary",
            ),
            a.known_by,
        )


def close_scene(builder: EventBuilder, transition_id: str) -> None:
    old_scene = builder.state.scene
    for a in list(builder.state.aspects.values()):
        if a.kind == "boost" or a.lifetime.kind == "scene_bound":
            builder.emit(
                AspectPayload(
                    kind="AspectRemoved", id=a.id, before=a, after=None, reason="scene ended"
                ),
                a.known_by,
            )
    for actor_id in old_scene.participant_ids:
        sheet = builder.state.entities[actor_id].fate
        assert sheet
        for track in ("physical", "mental"):
            used = getattr(sheet, track + "_stress_used")
            if used:
                builder.emit(StressPayload(actor_id=actor_id, track=track, before=used, after=0))
    session = builder.state.session
    new_session = session.model_copy(deep=True)
    new_session.completed_scene_ids.append(old_scene.id)
    for award in old_scene.deferred_awards:
        if award.subject_id == PC:
            sheet = builder.state.entities[PC].fate
            assert sheet
            builder.emit(
                PointsPayload(
                    account=PC,
                    before=sheet.fate_points,
                    after=sheet.fate_points + award.amount,
                    reason="hostile_award",
                ),
                [PC],
            )
        else:
            new_session.npc_carryover_credits[award.subject_id] = (
                new_session.npc_carryover_credits.get(award.subject_id, 0) + award.amount
            )
    recover_consequences(builder, closing_scene=True)
    builder.emit(
        SessionPayload(
            kind="SessionChanged", before=session, after=new_session, transition_id=transition_id
        )
    )
    builder.emit(
        ScenePayload(
            kind="SceneEnded",
            before=builder.state.scene,
            after=builder.state.scene.model_copy(update={"gm_fate_pool": 0, "deferred_awards": []}),
            transition_id=transition_id,
        )
    )


def start_scene(builder: EventBuilder, definition: str, transition_id: str) -> None:
    loc = location_of(builder.state, PC)
    participants = sorted(
        a for a in builder.state.knowledge if location_of(builder.state, a) == loc
    )
    session = builder.state.session
    after_session = session.model_copy(deep=True)
    credit = sum(after_session.npc_carryover_credits.pop(a, 0) for a in participants if a != PC)
    if after_session != session:
        builder.emit(
            SessionPayload(
                kind="SessionChanged",
                before=session,
                after=after_session,
                transition_id=transition_id,
            )
        )
    sequence = builder.state.scene.sequence + 1
    scene = SceneState(
        id=f"scene:{definition}:{sequence}",
        definition_ref=definition,
        sequence=sequence,
        participant_ids=participants,
        gm_fate_pool=1 + credit,
        started_tick=builder.state.clock.tick,
    )
    builder.emit(
        ScenePayload(
            kind="SceneStarted",
            before=builder.state.scene,
            after=scene,
            transition_id=transition_id,
        ),
        [PC],
    )


def boundaries(builder: EventBuilder) -> None:
    if builder.state.conflict is not None:
        return
    loc = location_of(builder.state, PC)
    definition = {
        "location:gatehouse": "scene:gate",
        "location:courtyard": "scene:courtyard",
        "location:records": "scene:records",
    }[loc or "location:gatehouse"]
    terminal = builder.state.quests["recover_ledger"].terminal_result is not None
    if builder.state.scene.id not in builder.state.session.completed_scene_ids and (
        builder.state.scene.definition_ref != definition
        or (terminal and not builder.state.session.finished)
    ):
        id = builder.allocate("boundary")
        close_scene(builder, id)
        if not terminal:
            start_scene(builder, definition, id)
    if terminal and not builder.state.session.finished:
        session = builder.state.session
        after = session.model_copy(update={"finished": True})
        recover_consequences(builder, closing_session=True)
        builder.emit(
            SessionPayload(
                kind="SessionChanged",
                before=session,
                after=after,
                transition_id="authored.session.finish",
            )
        )


def start_session(builder: EventBuilder, transition_id: str) -> None:
    if transition_id in builder.state.session.applied_boundary_ids:
        return
    session = builder.state.session
    after = session.model_copy(deep=True)
    after.sequence += 1
    after.id = f"session:{after.sequence}"
    after.finished = False
    after.applied_boundary_ids.append(transition_id)
    builder.emit(
        SessionPayload(
            kind="SessionStarted", before=session, after=after, transition_id=transition_id
        )
    )
    sheet = builder.state.entities[PC].fate
    assert sheet
    if sheet.fate_points < sheet.refresh:
        builder.emit(
            PointsPayload(
                account=PC, before=sheet.fate_points, after=sheet.refresh, reason="refresh"
            ),
            [PC],
        )


def breakthrough(builder: EventBuilder, id: str) -> None:
    builder.emit(TriggerPayload(kind="BreakthroughReached", id=id))
    recover_consequences(builder, breakthrough=True)
