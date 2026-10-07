"""Pure choice generation and validation for persisted interactions."""

from typing import Literal

from world_game.domain.common import GameError, digest
from world_game.domain.intents import ChoiceCommand, ChoiceOption, PendingResolution
from world_game.domain.state import WorldState
from world_game.engine.actions import PC
from world_game.engine.builder import EventBuilder
from world_game.engine.fate_rules import absorption_choices
from world_game.engine.queries import grounding_holds, location_of


def option(id: str, label: str, kind: str, **kwargs: object) -> ChoiceOption:
    return ChoiceOption(
        id=id, label=label, command=ChoiceCommand.model_validate({"kind": kind, **kwargs})
    )


def totals(builder: EventBuilder) -> tuple[int, int, int]:
    p = builder.pending
    plan = p.resolution_plan
    assert plan and plan.skill and plan.opposition and p.actor_dice
    actor = builder.state.entities[plan.actor_id].fate
    assert actor
    effort = sum(p.actor_dice) + actor.skills.get(plan.skill, 0) + plan.stunt_bonus + p.actor_bonus
    opposition = plan.opposition.difficulty
    if plan.opposition.kind == "active":
        sheet = builder.state.entities[plan.opposition.defender_id or ""].fate
        assert sheet and p.defender_dice
        opposition = (
            sum(p.defender_dice) + sheet.skills.get(plan.opposition.skill, 0) + p.defender_bonus
        )
    return effort, opposition, effort - opposition


def relevant(builder: EventBuilder, aspect_id: str, actor_id: str) -> bool:
    a = builder.state.aspects.get(aspect_id)
    plan = builder.pending.resolution_plan
    if not a or not plan or actor_id not in a.known_by or not grounding_holds(builder.state, a):
        return False
    if a.scope.kind == "location" and a.scope.id != location_of(builder.state, actor_id):
        return False
    if a.scope.kind == "scene" and a.scope.id != builder.state.scene.id:
        return False
    if a.scope.kind == "conversation" and (
        a.scope.id not in builder.state.conversations
        or actor_id not in builder.state.conversations[a.scope.id].participant_ids
    ):
        return False
    tags = {
        plan.goal,
        plan.method,
        plan.skill or "",
        plan.handler_id,
        "defend" if actor_id != plan.actor_id else "",
    }
    if plan.handler_id in ("social_overcome", "discover_advantage"):
        tags.add("social")
    if plan.handler_id in ("pick_lock", "use_key"):
        tags.update(["lock", "access"])
    return "any" in a.relevance_tags or bool(tags.intersection(a.relevance_tags))


def invoke_options(builder: EventBuilder, actor_id: str) -> list[ChoiceOption]:
    p = builder.pending
    plan = p.resolution_plan
    assert plan
    side: Literal["actor", "defender"] = "actor" if actor_id == plan.actor_id else "defender"
    sheet = builder.state.entities[actor_id].fate
    assert sheet
    balance = sheet.fate_points if actor_id == PC else builder.state.scene.gm_fate_pool
    options = []
    for a in sorted(builder.state.aspects.values(), key=lambda a: a.id):
        if not relevant(builder, a.id, actor_id):
            continue
        used = any(
            u.side == side and u.payment == "paid" and u.aspect_id == a.id for u in p.invoke_ledger
        )
        payments = (["free"] if a.free_invokes.get(actor_id, 0) else []) + (
            ["paid"] if a.kind != "boost" and balance > 0 and not used else []
        )
        for payment in payments:
            for mode in ("bonus", "reroll") if actor_id == PC else ("bonus",):
                options.append(
                    option(
                        f"invoke:{a.id}:{payment}:{mode}",
                        f"{a.text}: {'+2' if mode == 'bonus' else 'replace four dice'} ({payment})",
                        "invoke",
                        aspect_id=a.id,
                        mode=mode,
                        payment=payment,
                    )
                )
    return options


def legal_choices(state: WorldState, pending: PendingResolution) -> list[ChoiceOption]:
    if pending.current_choice is None:
        return []
    if pending.current_choice.kind == "invoke":
        return [
            option("pass", "Accept the current result", "pass"),
            *invoke_options(EventBuilder(state, pending), PC),
        ]
    return pending.current_choice.options


def validate_pending(state: WorldState, pending: PendingResolution) -> None:
    """Reject corrupted draws or substituted commands before resuming/importing."""

    def check(condition: bool, message: str) -> None:
        if not condition:
            raise GameError("Pending interaction integrity failure: " + message)

    check(pending.base_world_version == state.world_version, "stale world version")
    check(pending.actor_id in state.knowledge, "unavailable actor")
    b = EventBuilder(state, pending)
    check(pending.staged_rng.seed == state.rng.seed, "dice seed changed")
    counter = state.rng.counter
    for draw in pending.recorded_rolls:
        check(
            draw.before_counter == counter and draw.after_counter >= counter + 4,
            "draw continuation mismatch",
        )
        check(draw.check_id == f"check:{pending.input_id}", "draw check changed")
        counter = draw.after_counter
    check(pending.staged_rng.counter == counter, "staged dice continuation mismatch")
    for side, dice in (("actor", pending.actor_dice), ("defender", pending.defender_dice)):
        draws = [d for d in pending.recorded_rolls if d.side == side]
        check(
            dice == (draws[-1].dice if draws else None), "displayed dice differ from draw history"
        )
        check([d.reroll_index for d in draws] == list(range(len(draws))), "reroll indices differ")
        paid = [
            u.aspect_id for u in pending.invoke_ledger if u.side == side and u.payment == "paid"
        ]
        check(len(paid) == len(set(paid)), "duplicate paid invoke")
        bonus = 2 * sum(u.side == side and u.mode == "bonus" for u in pending.invoke_ledger)
        check(
            bonus == (pending.actor_bonus if side == "actor" else pending.defender_bonus),
            "invoke bonus mismatch",
        )
    choice = pending.current_choice
    if choice is None:
        return
    check(
        choice.context_hash
        == digest(
            {
                "state": digest(b.state),
                "rolls": pending.recorded_rolls,
                "revision": pending.choice_revision,
            }
        ),
        "choice context changed",
    )
    check(len({o.id for o in choice.options}) == len(choice.options), "duplicate option IDs")
    if choice.kind == "invoke":
        check(choice.options == legal_choices(state, pending), "invoke options changed")
    elif choice.kind == "harm":
        check(
            pending.defender_id is not None and pending.resolution_plan is not None,
            "harm check missing",
        )
        assert pending.defender_id and pending.resolution_plan
        sheet = b.state.entities[pending.defender_id].fate
        assert sheet
        legal = absorption_choices(sheet, pending.resolution_plan.harm_type, pending.damage)
        for o in choice.options:
            check(
                o.command == ChoiceCommand(kind="taken_out")
                or any(
                    o.command == ChoiceCommand(kind="absorb", stress=a.stress, slots=a.slots)
                    for a in legal
                ),
                "harm command changed",
            )
    elif choice.kind == "offer":
        check(
            all(
                o.command in (ChoiceCommand(kind="accept"), ChoiceCommand(kind="decline"))
                for o in choice.options
            ),
            "offer command changed",
        )
    elif choice.kind == "compel":
        sheet = b.state.entities[PC].fate
        assert sheet
        check(
            all(
                o.command
                in [
                    ChoiceCommand(kind="accept"),
                    ChoiceCommand(kind="object"),
                    *([ChoiceCommand(kind="refuse")] if sheet.fate_points else []),
                ]
                for o in choice.options
            ),
            "compel command changed",
        )
    elif choice.kind == "concession":
        check(
            not pending.recorded_rolls and b.state.conflict is not None, "concession timing changed"
        )
        check(
            all(
                o.command
                in [
                    ChoiceCommand(kind="concede", terms="custody"),
                    ChoiceCommand(kind="concede", terms="abandon"),
                ]
                for o in choice.options
            ),
            "concession terms changed",
        )
    elif choice.kind == "cost":
        check(
            all(
                o.command
                in [
                    ChoiceCommand(kind="keep"),
                    ChoiceCommand(kind="trade"),
                    ChoiceCommand(kind="major_cost"),
                    ChoiceCommand(kind="failure"),
                ]
                for o in choice.options
            ),
            "cost command changed",
        )
    elif choice.kind == "pre_roll":
        check(
            not pending.recorded_rolls
            and all(
                o.command in [ChoiceCommand(kind="continue"), ChoiceCommand(kind="concede")]
                for o in choice.options
            ),
            "pre-roll command changed",
        )
    elif choice.kind == "next_actor":
        check(
            all(
                o.command.kind == "next_actor"
                and o.command.target_id in pending.next_actor_candidates
                for o in choice.options
            ),
            "next actor changed",
        )
    elif choice.kind == "clarification":
        check(
            all(
                o.command.kind == "clarify" and o.command.target_id in b.state.entities
                for o in choice.options
            ),
            "clarification target changed",
        )
