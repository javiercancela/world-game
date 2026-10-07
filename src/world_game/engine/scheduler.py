from world_game.domain.common import GameError
from world_game.domain.events import MovePayload, SchedulePayload
from world_game.engine.actions import OREN, PC, change_belief, fail_quest, portal_change, say
from world_game.engine.builder import EventBuilder
from world_game.engine.queries import container, location_of


def run_due(builder: EventBuilder, limit: int = 20) -> None:
    processed = 0
    while True:
        due = sorted(
            (
                e
                for e in builder.state.scheduled_effects.values()
                if e.status == "pending" and e.due_tick <= builder.state.clock.tick
            ),
            key=lambda e: (e.due_tick, e.priority, e.id),
        )
        if not due:
            return
        processed += 1
        if processed > limit:
            raise GameError("Scheduled effect processing limit exceeded; pending action preserved.")
        effect = due[0]
        builder.actor_id = None
        builder.rule_id = "authored.schedule." + effect.handler_id
        terminal = builder.state.quests[effect.args.quest_id].terminal_result
        oren = builder.state.entities[OREN].actor
        unable_to_verify = effect.handler_id == "verify_invitation" and (
            oren is None or oren.status != "active"
        )
        status = "canceled" if terminal or unable_to_verify else "applied"
        if not terminal and effect.handler_id == "ledger_deadline":
            if container(builder.state, "item:ledger") == "location:records":
                builder.emit(
                    MovePayload(
                        kind="ItemTransferred",
                        entity_id="item:ledger",
                        from_id="location:records",
                        to_id="container:archive",
                        permission="scheduler",
                    )
                )
                fail_quest(
                    builder, "The ledger has been removed to a sealed archive. The mission fails."
                )
            else:
                status = "skipped"
        elif not terminal and not unable_to_verify and effect.handler_id == "verify_invitation":
            change_belief(builder, "disbelieves")
            portal_change(builder, "portal:gate", False)
            if location_of(builder.state, PC) != "location:gatehouse":
                old = location_of(builder.state, OREN)
                if old != "location:courtyard":
                    assert old
                    builder.emit(
                        MovePayload(
                            kind="EntityMoved",
                            entity_id=OREN,
                            from_id=old,
                            to_id="location:courtyard",
                            permission="confront",
                        ),
                        [PC] if location_of(builder.state, PC) == "location:courtyard" else [],
                    )
                if location_of(builder.state, PC) == "location:courtyard":
                    say(
                        builder,
                        OREN,
                        "The invitation could not be verified. Explain yourself.",
                        [PC],
                    )
            else:
                say(
                    builder,
                    OREN,
                    "Your invitation could not be verified. The gate is closed.",
                    [PC],
                )
        builder.emit(
            SchedulePayload(
                kind="ScheduledEffectResolved",
                id=effect.id,
                before=effect,
                after=effect.model_copy(update={"status": status}),
            )
        )
