from world_game.domain.events import (
    AspectPayload,
    ConflictPayload,
    ConsequencePayload,
    MovePayload,
    PointsPayload,
    StatusPayload,
    StressPayload,
    TimePayload,
)
from world_game.domain.fate import Aspect, Lifetime, Recovery, Scope, Slot, Source, TraitGrounding
from world_game.engine.actions import OREN, PC, SEN, add_aspect, fail_quest, portal_change
from world_game.engine.builder import EventBuilder
from world_game.engine.fate_rules import SEVERITIES, absorption_choices
from world_game.engine.queries import co_located, container, location_of
from world_game.engine.scenes import close_scene, start_scene


def absorb(
    builder: EventBuilder,
    defender: str,
    attacker: str,
    damage: int,
    stress: int,
    slots: list[Slot],
    catalog: dict[str, list[str]],
) -> None:
    plan = builder.pending.resolution_plan
    assert plan
    harm = plan.harm_type
    sheet = builder.state.entities[defender].fate
    assert sheet
    legal = absorption_choices(sheet, harm, damage)
    if not any(c.stress == stress and c.slots == slots for c in legal):
        from world_game.domain.common import GameError

        raise GameError("That allocation does not absorb the complete hit.")
    if stress:
        used = getattr(sheet, harm + "_stress_used")
        builder.emit(
            StressPayload(actor_id=defender, track=harm, before=used, after=used + stress),
            [PC, defender],
        )
    for slot in slots:
        severity = SEVERITIES[slot]
        id = builder.allocate("aspect")
        template = catalog[harm][[2, 4, 6].index(severity)]
        a = Aspect(
            id=id,
            text=template.replace("_", " ").title(),
            kind="consequence",
            subject_id=defender,
            scope=Scope(kind="character", id=defender),
            grounding=TraitGrounding(id="consequence:" + template),
            known_by=sorted(set([attacker, defender])),
            free_invokes={attacker: 1},
            lifetime=Lifetime(kind="persistent"),
            source=Source(
                kind="event",
                id=f"event:{builder.pending.input_id}:{len(builder.pending.staged_events)}",
            ),
            relevance_tags=["fight", "defend", "treatment"],
            recovery=Recovery(severity=severity, harm_type=harm, slot=slot),
        )
        builder.emit(AspectPayload(kind="AspectCreated", id=id, before=None, after=a), a.known_by)
        builder.emit(
            ConsequencePayload(actor_id=defender, slot=slot, before=None, after=id), a.known_by
        )
        conflict = builder.state.conflict
        if conflict:
            after = conflict.model_copy(deep=True)
            after.consequences_taken[defender] = after.consequences_taken.get(defender, 0) + 1
            builder.emit(
                ConflictPayload(
                    kind="ConflictAdvanced",
                    before=conflict,
                    after=after,
                    transition_id="authored.harm",
                ),
                [PC],
            )
    builder.pending.output_lines.append(
        f"{builder.state.entities[defender].identity.name} absorbs {damage} shifts using {stress} stress and {', '.join(slots) or 'no consequences'}."
    )


def taken_out(builder: EventBuilder, defender: str) -> None:
    actor = builder.state.entities[defender].actor
    assert actor
    builder.emit(StatusPayload(actor_id=defender, before=actor.status, after="taken_out"), [PC])
    if defender == PC:
        fail_quest(
            builder,
            "Oren's custody closes around you. You are captured; the mission fails."
            if builder.pending.actor_id == OREN
            else "Sen restrains you. You are captured; the mission fails.",
        )
    else:
        builder.pending.output_lines.append(
            f"{builder.state.entities[defender].identity.name} can no longer interfere during this adventure."
        )
        if defender == OREN:
            portal_change(builder, "portal:gate", True)
        elif (
            defender == SEN
            and container(builder.state, "item:records_key") == SEN
            and co_located(builder.state, PC, SEN)
        ):
            builder.emit(
                MovePayload(
                    kind="ItemTransferred",
                    entity_id="item:records_key",
                    from_id=SEN,
                    to_id=PC,
                    permission="conflict",
                ),
                [PC],
            )
    end_conflict(builder)


def concede(builder: EventBuilder, terms: str) -> None:
    conflict = builder.state.conflict
    assert conflict
    actor = builder.state.entities[PC].actor
    assert actor
    builder.emit(StatusPayload(actor_id=PC, before=actor.status, after="conceded"), [PC])
    after = conflict.model_copy(deep=True)
    after.pending_concession_awards[PC] = 1 + after.consequences_taken.get(PC, 0)
    builder.emit(
        ConflictPayload(
            kind="ConflictAdvanced",
            before=conflict,
            after=after,
            transition_id="authored.concession",
        ),
        [PC],
    )
    fail_quest(
        builder,
        "You surrender into custody."
        if terms == "custody"
        else "You abandon the ledger mission and leave with your personal belongings.",
    )
    if terms == "abandon":
        location = location_of(builder.state, PC)
        assert location
        if container(builder.state, "item:ledger") == PC:
            builder.emit(
                MovePayload(
                    kind="ItemTransferred",
                    entity_id="item:ledger",
                    from_id=PC,
                    to_id=location,
                    permission="concession",
                ),
                [PC],
            )
        if location != "location:gatehouse":
            builder.emit(
                MovePayload(
                    kind="EntityMoved",
                    entity_id=PC,
                    from_id=location,
                    to_id="location:gatehouse",
                    permission="concession",
                ),
                [PC],
            )
    end_conflict(builder)


def end_conflict(builder: EventBuilder) -> None:
    conflict = builder.state.conflict
    if not conflict:
        return
    if conflict.last_charged_exchange < conflict.exchange:
        before = builder.state.clock
        builder.emit(
            TimePayload(before=before, after=before.model_copy(update={"tick": before.tick + 1}))
        )
    for actor_id, award in conflict.pending_concession_awards.items():
        sheet = builder.state.entities[actor_id].fate
        assert sheet
        builder.emit(
            PointsPayload(
                account=actor_id,
                before=sheet.fate_points,
                after=sheet.fate_points + award,
                reason="concession",
            ),
            [actor_id],
        )
    builder.emit(
        ConflictPayload(
            kind="ConflictEnded",
            before=builder.state.conflict,
            after=None,
            transition_id="authored.conflict.end",
        ),
        [PC],
    )
    close_scene(builder, "authored.conflict.end")
    if builder.state.quests["recover_ledger"].terminal_result is None:
        start_scene(builder, builder.state.scene.definition_ref, "authored.conflict.end")


def advance_conflict(builder: EventBuilder, next_actor: str | None = None) -> list[str]:
    conflict = builder.state.conflict
    assert conflict
    actor = builder.pending.actor_id
    after = conflict.model_copy(deep=True)
    after.acted_ids = sorted(set([*after.acted_ids, actor]))
    active = sorted(
        p.actor_id
        for p in after.participants
        if builder.state.entities[p.actor_id].actor
        and builder.state.entities[p.actor_id].actor.status == "active"  # type: ignore[union-attr]
    )
    eligible = [a for a in active if a not in after.acted_ids]
    new_exchange = not eligible
    if new_exchange:
        eligible = active
    if next_actor is None and actor == PC and len(eligible) > 1:
        return eligible
    if new_exchange:
        if after.last_charged_exchange < after.exchange:
            before = builder.state.clock
            builder.emit(
                TimePayload(
                    before=before, after=before.model_copy(update={"tick": before.tick + 1})
                )
            )
            after.last_charged_exchange = after.exchange
        after.exchange += 1
        after.acted_ids = []
    after.active_actor_id = next_actor or eligible[0]
    builder.emit(
        ConflictPayload(
            kind="ConflictAdvanced",
            before=conflict,
            after=after,
            transition_id="authored.turn_order",
        ),
        [PC],
    )
    return []


def attack_outcome(builder: EventBuilder, margin: int, trade: bool = False) -> int:
    plan = builder.pending.resolution_plan
    assert plan and plan.opposition and plan.target_id
    if margin <= 0:
        if margin == 0:
            add_aspect(builder, plan.actor_id, "momentary_opening", controller=plan.actor_id)
        elif margin <= -3:
            add_aspect(builder, plan.target_id, "momentary_opening", controller=plan.target_id)
        builder.pending.output_lines.append("The attack causes no harm.")
        return 0
    if margin >= 3 and trade:
        add_aspect(builder, plan.actor_id, "momentary_opening", controller=plan.actor_id)
        return margin - 1
    return margin
