"""Registered action handlers: permissions and outcomes remain engine-owned."""

from typing import Literal, cast

from world_game.domain.common import GameError, Value
from world_game.domain.events import (
    AspectPayload,
    BeliefPayload,
    CommitmentPayload,
    ConflictPayload,
    ConversationPayload,
    MovePayload,
    OfferPayload,
    PortalPayload,
    QuestPayload,
    RelationshipPayload,
    SchedulePayload,
    TriggerPayload,
    UtterancePayload,
)
from world_game.domain.fate import (
    Aspect,
    AspectTemplate,
    ComponentGrounding,
    ConflictParticipant,
    ConflictState,
    Lifetime,
    Opposition,
    ResolutionPlan,
    Scope,
    Source,
    TraitGrounding,
)
from world_game.domain.intents import Intent
from world_game.domain.state import (
    Belief,
    Commitment,
    Conversation,
    EffectArguments,
    Fulfillment,
    Offer,
    OfferTerms,
    ScheduledEffect,
    TransferTerm,
    WorldState,
)
from world_game.engine.builder import EventBuilder
from world_game.engine.fate_rules import outcome_branch
from world_game.engine.queries import co_located, container, grounding_holds, location_of, possesses
from world_game.story.schema import AtomicPredicate, BooleanPredicate, Predicate, Rule, Rules

PC = "actor:lea"
OREN = "actor:oren"
SEN = "actor:sen"


class Ruling(Value):
    rules: Rules


def blocker(condition: bool, message: str) -> None:
    if not condition:
        raise GameError(message)


def target(intent: Intent, index: int = 0) -> str:
    return intent.target_ids[index] if len(intent.target_ids) > index else ""


def has_tool(state: WorldState, actor: str, tag: str, presented: list[str] | None = None) -> bool:
    return any(
        e.item
        and tag in e.item.tags
        and possesses(state, actor, e.id)
        and (presented is None or e.id in presented)
        for e in state.entities.values()
    )


def evaluate_predicate(state: WorldState, predicate: Predicate) -> bool:
    if isinstance(predicate, BooleanPredicate):
        values = [evaluate_predicate(state, child) for child in predicate.children]
        return (
            all(values)
            if predicate.kind == "all"
            else any(values)
            if predicate.kind == "any"
            else not values[0]
        )
    assert isinstance(predicate, AtomicPredicate)
    a, b = predicate.subject_id, predicate.object_id or ""
    if predicate.kind == "co_located":
        return co_located(state, a, b)
    if predicate.kind == "possesses":
        return possesses(state, a, b)
    if predicate.kind == "portal_state":
        entity = state.entities.get(a)
        return bool(
            entity
            and entity.portal
            and getattr(entity.portal, predicate.portal_field) == predicate.value
        )
    if predicate.kind == "has_known_belief":
        return any(
            belief.holder_id == a
            and belief.proposition_id == b
            and belief.stance == predicate.value
            for belief in state.beliefs.values()
        )
    if predicate.kind == "aspect_active":
        aspect = state.aspects.get(a)
        return bool(aspect and grounding_holds(state, aspect))
    if predicate.kind == "quest_stage":
        return a in state.quests and state.quests[a].stage == predicate.value
    return a in state.conversations and b in state.conversations[a].participant_ids


def select_rule(intent: Intent, rules: Rules) -> Rule:
    if intent.handler_id == "social_overcome":
        id = (
            "gate.exit"
            if intent.goal == "exit"
            else "gate.concern"
            if intent.method == "address_concern" or intent.goal == "address_concern"
            else "gate.bluff"
        )
    elif intent.handler_id in (
        "discover_advantage",
        "create_advantage",
        "attack",
        "pick_lock",
        "treat_consequence",
    ):
        id = {
            "discover_advantage": "read.concern",
            "create_advantage": "advantage.new",
            "attack": "attack.physical",
            "pick_lock": "lock.pick",
            "treat_consequence": "treat",
        }[intent.handler_id]
    else:
        id = "action." + intent.handler_id
    return next(r for r in rules.actions if r.id == id)


def plan_action(
    state: WorldState, intent: Intent, ruling: Ruling, actor_id: str = PC
) -> ResolutionPlan:
    blocker(not intent.unresolved_references, "Please clarify the target first.")
    blocker(state.quests["recover_ledger"].terminal_result is None, "This adventure has ended.")
    if state.conflict:
        blocker(state.conflict.active_actor_id == actor_id, "Wait for the current conflict actor.")
        blocker(
            intent.handler_id
            in (
                "attack",
                "create_advantage",
                "discover_advantage",
                "move",
                "wait",
                "treat_consequence",
            ),
            "During conflict, choose an action, advantage, movement, or concession.",
        )
    for ref in intent.target_ids:
        blocker(
            ref in state.entities or ref in state.aspects or ref in state.offers,
            "That reference is unavailable.",
        )
    for item in intent.presented_item_ids:
        blocker(possesses(state, actor_id, item), "You must possess an item to present it.")
    rule = select_rule(intent, ruling.rules)
    blocker(
        all(evaluate_predicate(state, condition) for condition in rule.preconditions),
        "This attempt needs a different fictional opportunity.",
    )
    t = target(intent)
    if intent.handler_id in (
        "speak",
        "social_overcome",
        "discover_advantage",
        "attack",
        "present_item",
    ):
        blocker(
            t in state.entities
            and state.entities[t].actor is not None
            and t != actor_id
            and co_located(state, actor_id, t),
            "That person is not here.",
        )
        target_actor = state.entities[t].actor
        blocker(
            target_actor is not None and target_actor.status == "active",
            "That person cannot respond.",
        )
    if intent.handler_id == "social_overcome":
        blocker(t == OREN, "Sen already recognizes you; ask her for help.")
        if rule.id == "gate.bluff":
            blocker(
                intent.goal in ("", "gain_entry")
                and intent.method in ("", "claim_expected_courier"),
                "Choose a supported claim of authorization or an honest request to Oren.",
            )
            blocker(
                "gate.bluff.failed" not in state.applied_trigger_ids,
                "Oren requires new evidence; the unchanged claim cannot be rolled again.",
            )
        if rule.id == "gate.concern":
            blocker(
                actor_id in state.aspects["aspect:oren:blame"].known_by,
                "Discover Oren's concern before addressing it.",
            )
    if intent.handler_id == "move":
        blocker(
            t in state.entities and state.entities[t].location is not None,
            "Choose an adjacent location.",
        )
        portals = [
            e
            for e in state.entities.values()
            if e.portal and set(e.portal.endpoints) == {location_of(state, actor_id), t}
        ]
        blocker(bool(portals), "There is no adjacent passage there.")
        blocker(
            not any(
                a.template_id == "jammed_portal"
                and a.grounding.kind == "component_predicate"
                and a.grounding.value_id in {e.id for e in portals}
                and grounding_holds(state, a)
                for a in state.aspects.values()
            ),
            "An object wedged in the passage blocks your movement.",
        )
        blocker(
            any(e.portal and e.portal.open and not e.portal.locked for e in portals),
            "The passage is closed or locked.",
        )
    if intent.handler_id in ("use_key", "pick_lock"):
        blocker(
            t in state.entities
            and state.entities[t].portal is not None
            and location_of(state, actor_id) in state.entities[t].portal.endpoints,  # type: ignore[union-attr]
            "The lock is out of reach.",
        )
        portal = state.entities[t].portal
        assert portal
        if intent.handler_id == "use_key":
            blocker(
                any(possesses(state, actor_id, key) for key in portal.key_item_ids),
                "You need the correct key.",
            )
        else:
            blocker(
                "mechanical_lock" in portal.tags and portal.locked,
                "There is no locked mechanism to pick.",
            )
            blocker(has_tool(state, actor_id, "lockpick"), "You need your lockpicks.")
    if intent.handler_id == "take_item":
        blocker(
            t in state.entities
            and state.entities[t].item is not None
            and container(state, t) == location_of(state, actor_id),
            "That item is not available here.",
        )
    if intent.handler_id == "retrieve_lodged_item":
        holder = container(state, t) if t in state.entities else None
        blocker(
            holder is not None
            and state.entities[holder].portal is not None
            and location_of(state, actor_id) in state.entities[holder].portal.endpoints,  # type: ignore[union-attr]
            "That item is not lodged within reach.",
        )
    if intent.handler_id == "transfer_item":
        recipient = target(intent, 1)
        blocker(
            possesses(state, actor_id, t)
            and recipient in state.entities
            and co_located(state, actor_id, recipient),
            "Transfer requires possession and a recipient here.",
        )
        blocker(
            (t == "item:records_key" and recipient == SEN)
            or (t == "item:archive_receipt" and recipient == OREN),
            "That recipient has not accepted this transfer.",
        )
    if intent.handler_id == "create_advantage":
        blocker(
            intent.method
            in ("off_balance", "focused", "cover", "jammed_portal", "enhance", "burning"),
            "Use an advantage grounded in positioning, focus, cover, or a carried object.",
        )
        if t in state.aspects:
            blocker(actor_id in state.aspects[t].known_by, "That aspect is unavailable.")
        elif t in state.entities and state.entities[t].actor:
            blocker(co_located(state, actor_id, t), "That person is not here.")
        if intent.method == "burning":
            burnable = state.entities.get(t)
            blocker(
                burnable is not None
                and burnable.item is not None
                and "burning" in burnable.item.tags
                and co_located(state, actor_id, t),
                "A fire advantage requires a visibly burning object here.",
            )
        if intent.method == "jammed_portal":
            blocker(
                t in state.entities
                and state.entities[t].portal is not None
                and location_of(state, actor_id) in state.entities[t].portal.endpoints  # type: ignore[union-attr]
                and bool(intent.presented_item_ids),
                "A wedge needs a carried object and a reachable portal.",
            )
    if intent.handler_id == "treat_consequence":
        blocker(
            t in state.aspects
            and state.aspects[t].recovery is not None
            and actor_id in state.aspects[t].known_by
            and co_located(state, actor_id, state.aspects[t].subject_id),
            "No treatable consequence is available here.",
        )
    check = rule.check
    action = check.action
    skill = check.skill_id
    opposition = (
        None
        if check.kind == "none"
        else Opposition(
            kind="active" if check.kind == "active" else "static",
            difficulty=check.difficulty,
            defender_id=t if check.kind == "active" else None,
            skill=check.defense_skill,
        )
    )
    if intent.handler_id == "attack":
        blocker(
            intent.method != "mental" or intent.goal == "authored_harmful_confrontation",
            "Ordinary conversation is not a mental attack.",
        )
        blocker(
            intent.method != "mental",
            "This adventure has no authored harmful mental confrontation.",
        )
        opposition = Opposition(
            kind="active", defender_id=t, skill="athletics" if t == PC else "fight"
        )
    if intent.handler_id == "create_advantage" and t in state.entities and state.entities[t].actor:
        opposition = Opposition(kind="active", defender_id=t, skill="athletics")
        skill = "fight"
    if intent.handler_id == "treat_consequence":
        recovery = state.aspects[t].recovery
        assert recovery
        skill = "craft" if recovery.harm_type == "physical" else "influence"
        opposition = Opposition(
            kind="static",
            difficulty=recovery.severity + (2 if state.aspects[t].subject_id == actor_id else 0),
        )

    eligible: list[str] = []
    sheet = state.entities[actor_id].fate
    assert sheet
    for stunt in ruling.rules.stunts:
        if (
            stunt.id not in rule.permitted_stunt_ids
            or stunt.id not in sheet.stunt_ids
            or stunt.action != action
            or stunt.skill_id != skill
            or stunt.handler_id != intent.handler_id
            or stunt.goal != (intent.goal or check.goal_tag)
        ):
            continue
        if stunt.item_tag and not has_tool(
            state,
            actor_id,
            stunt.item_tag,
            intent.presented_item_ids if stunt.id == "official_bearing" else None,
        ):
            continue
        if stunt.requires_conversation and not any(
            actor_id in c.participant_ids
            and t in c.participant_ids
            and c.location_id == location_of(state, actor_id)
            for c in state.conversations.values()
        ):
            continue
        eligible.append(stunt.id)
    unknown = (
        intent.handler_id == "discover_advantage"
        and actor_id not in state.aspects["aspect:oren:blame"].known_by
    )
    blocker(
        intent.handler_id != "discover_advantage" or t == OREN,
        "There is no authored undiscovered concern for that person.",
    )
    return ResolutionPlan(
        rule_id=rule.id,
        handler_id=intent.handler_id,
        actor_id=actor_id,
        target_id=t or None,
        action=action,
        skill=skill,
        opposition=opposition,
        eligible_stunts=eligible,
        stunt_bonus=2 * len(eligible),
        time_cost=rule.cost_ticks,
        goal=intent.goal or check.goal_tag,
        method=intent.method,
        advantage_kind="unknown"
        if unknown
        else "known"
        if intent.handler_id == "discover_advantage" or t in state.aspects
        else "new",
        aspect_id="aspect:oren:blame"
        if intent.handler_id == "discover_advantage"
        else t
        if t in state.aspects
        else None,
        stakes="Entry may be refused or verified."
        if rule.id == "gate.bluff"
        else "Failure at picking the lock makes noise; a tie lodges your tool."
        if intent.handler_id == "pick_lock"
        else "Defeat means capture."
        if intent.handler_id == "attack"
        else "The attempt costs one action.",
        failure_template_id=rule.outcomes.failure,
        minor_cost_template_id=rule.outcomes.minor_cost,
        major_cost_template_id=rule.outcomes.optional_major_cost,
    )


def conversation(builder: EventBuilder, actor_id: str, other_id: str, topic: str) -> None:
    loc = location_of(builder.state, actor_id)
    assert loc
    id = "conversation:" + ":".join(sorted([actor_id.split(":")[-1], other_id.split(":")[-1]]))
    before = builder.state.conversations.get(id)
    after = Conversation(
        id=id,
        participant_ids=sorted([actor_id, other_id]),
        location_id=loc,
        topic=topic,
        recent_utterance_ids=before.recent_utterance_ids if before else [],
        offer_ids=before.offer_ids if before else [],
    )
    builder.emit(ConversationPayload(id=id, before=before, after=after), [actor_id, other_id])


def say(
    builder: EventBuilder,
    speaker: str,
    text: str,
    audience: list[str],
    disclosures: list[str] | None = None,
) -> None:
    builder.emit(
        UtterancePayload(
            speaker_id=speaker, text=text, audience_ids=audience, disclosures=disclosures or []
        ),
        sorted([speaker, *audience]),
    )
    if PC == speaker or PC in audience:
        name = builder.state.entities[speaker].identity.name
        builder.pending.output_lines.append(f"{name}: {text}")


def portal_change(builder: EventBuilder, id: str, open: bool, locked: bool = False) -> None:
    before = builder.state.entities[id].portal
    assert before
    after = before.model_copy(update={"open": open, "locked": locked})
    if before != after:
        builder.emit(
            PortalPayload(portal_id=id, before=before, after=after),
            [PC, OREN] if id == "portal:gate" else [PC],
        )


def change_belief(builder: EventBuilder, stance: Literal["believes", "disbelieves"]) -> None:
    id = "belief:oren:invitation"
    before = builder.state.beliefs.get(id)
    after = Belief(
        id=id,
        holder_id=OREN,
        proposition_id="proposition:lea_expected",
        stance=stance,
        source_event_ids=[builder.pending.staged_events[-1].id],
        acquired_tick=builder.state.clock.tick,
    )
    builder.emit(BeliefPayload(id=id, before=before, after=after), [OREN])
    if stance != "believes":
        for a in list(builder.state.aspects.values()):
            if a.grounding.kind == "belief" and a.grounding.id == id:
                builder.emit(
                    AspectPayload(
                        kind="AspectRemoved",
                        id=a.id,
                        before=a,
                        after=None,
                        reason="supporting belief changed",
                    )
                )


def add_aspect(
    builder: EventBuilder,
    subject: str,
    template: str,
    uses: int = 1,
    kind: Literal["boost", "situation"] = "boost",
    controller: str = PC,
    grounding: ComponentGrounding | None = None,
) -> str:
    id = builder.allocate("aspect")
    template = (
        template
        if template
        in (
            "momentary_opening",
            "noisy_intrusion",
            "off_balance",
            "focused",
            "cover",
            "jammed_portal",
            "burning",
        )
        else "momentary_opening"
    )
    text = {
        "momentary_opening": "Momentary Opening",
        "noisy_intrusion": "Noisy Intrusion",
        "off_balance": "Off Balance",
        "focused": "Focused",
        "cover": "Sheltered by Cover",
        "jammed_portal": "Jammed Portal",
        "burning": "Burning",
    }.get(template, "Momentary Opening")
    aspect = Aspect(
        id=id,
        text=text,
        kind=kind,
        subject_id=subject,
        scope=Scope(
            kind="location",
            id=location_of(builder.state, builder.pending.actor_id) or "location:gatehouse",
        )
        if grounding
        else Scope(kind="scene", id=builder.state.scene.id),
        grounding=grounding or TraitGrounding(id="template:" + template),
        known_by=sorted(
            set(
                [PC, controller, subject]
                if subject in builder.state.knowledge
                else [PC, controller]
            )
        ),
        free_invokes={controller: uses},
        lifetime=Lifetime(
            kind="until_grounding_false"
            if grounding
            else "next_eligible_use"
            if kind == "boost"
            else "scene_bound"
        ),
        source=Source(
            kind="event",
            id=f"event:{builder.pending.input_id}:{len(builder.pending.staged_events)}",
        ),
        template_id=cast(AspectTemplate, template),
        relevance_tags=["any"],
    )
    builder.emit(
        AspectPayload(kind="AspectCreated", id=id, before=None, after=aspect), aspect.known_by
    )
    builder.pending.output_lines.append(
        f"{text}: {uses} free invoke(s) for {builder.state.entities[controller].identity.name}."
    )
    return id


def offer(
    builder: EventBuilder, proposer: str, template: Literal["collateral", "key_loan"]
) -> Offer:
    id = builder.allocate("offer")
    item = "item:royal_seal" if template == "collateral" else "item:records_key"
    from_id, to_id = (PC, OREN) if template == "collateral" else (SEN, PC)
    obligation = Commitment(
        id=builder.allocate("commitment"),
        debtor_id=OREN if template == "collateral" else PC,
        creditor_id=PC if template == "collateral" else SEN,
        fulfillment_predicate=Fulfillment(
            kind="returned", item_id=item, recipient_id=PC if template == "collateral" else SEN
        ),
        breach_trigger_id="return_with_ledger" if template == "collateral" else "leave_with_key",
        source_event_id=f"event:{builder.pending.input_id}:{len(builder.pending.staged_events)}",
    )
    o = Offer(
        id=id,
        proposer_id=proposer,
        recipient_id=PC,
        terms=OfferTerms(
            template_id=template,
            transfers=[TransferTerm(item_id=item, from_id=from_id, to_id=to_id)],
            commitment=obligation,
            portal_id="portal:gate" if template == "collateral" else None,
        ),
        expires_tick=builder.state.clock.tick + 5,
        source_event_id=f"event:{builder.pending.input_id}:{len(builder.pending.staged_events)}",
    )
    builder.emit(OfferPayload(kind="OfferMade", id=id, before=None, after=o), [PC, proposer])
    return o


def accept_offer(builder: EventBuilder, id: str, accepted: bool) -> None:
    old = builder.state.offers[id]
    blocker(
        old.status == "pending"
        and old.expires_tick >= builder.state.clock.tick
        and co_located(builder.state, old.proposer_id, old.recipient_id),
        "That offer is no longer available.",
    )
    new = old.model_copy(update={"status": "accepted" if accepted else "declined"})
    builder.emit(
        OfferPayload(kind="OfferResolved", id=id, before=old, after=new), [PC, old.proposer_id]
    )
    if accepted:
        for term in old.terms.transfers:
            builder.emit(
                MovePayload(
                    kind="ItemTransferred",
                    entity_id=term.item_id,
                    from_id=term.from_id,
                    to_id=term.to_id,
                    permission="offer",
                ),
                [PC, old.proposer_id],
            )
        builder.emit(
            CommitmentPayload(
                kind="CommitmentMade",
                id=old.terms.commitment.id,
                before=None,
                after=old.terms.commitment,
            ),
            [PC, old.proposer_id],
        )
        if old.terms.portal_id:
            portal_change(builder, old.terms.portal_id, True)
        builder.pending.output_lines.append(
            "Offer accepted; the transfer and promise are committed together."
        )
    else:
        builder.pending.output_lines.append("Offer declined.")


def start_conflict(builder: EventBuilder, opponent: str) -> None:
    if builder.state.conflict:
        return
    c = ConflictState(
        id=builder.allocate("conflict"),
        scene_id=builder.state.scene.id,
        participants=[
            ConflictParticipant(actor_id=PC, side_id="player"),
            ConflictParticipant(actor_id=opponent, side_id="opposition"),
        ],
        active_actor_id=builder.pending.actor_id,
    )
    builder.emit(
        ConflictPayload(
            kind="ConflictStarted", before=None, after=c, transition_id="authored.nonlethal"
        ),
        [PC, opponent],
    )
    builder.pending.output_lines.append(
        "Conflict begins: escape or capture. Each participant acts once per exchange."
    )


def action_effects(builder: EventBuilder, margin: int | None = None) -> None:
    p = builder.pending
    plan = p.resolution_plan
    intent = p.interpreted_intent
    assert plan and intent
    t = plan.target_id or ""
    h = plan.handler_id
    state = builder.state
    if h == "wait":
        p.output_lines.append("You wait as dusk deepens.")
    elif h == "move":
        loc = location_of(state, plan.actor_id)
        portal = next(
            e for e in state.entities.values() if e.portal and set(e.portal.endpoints) == {loc, t}
        )
        assert loc
        builder.emit(
            MovePayload(
                kind="EntityMoved",
                entity_id=plan.actor_id,
                from_id=loc,
                to_id=t,
                permission="traverse",
                portal_id=portal.id,
            ),
            [PC],
        )
        p.output_lines.append(f"You move to {state.entities[t].identity.name}.")
    elif h in ("take_item", "transfer_item", "retrieve_lodged_item"):
        recipient = target(intent, 1) if h == "transfer_item" else plan.actor_id
        holder = container(state, t)
        assert holder
        builder.emit(
            MovePayload(
                kind="ItemTransferred",
                entity_id=t,
                from_id=holder,
                to_id=recipient,
                permission="give"
                if h == "transfer_item"
                else "retrieve"
                if h == "retrieve_lodged_item"
                else "take",
            ),
            [PC, recipient] if recipient != PC else [PC],
        )
        p.output_lines.append(
            f"{state.entities[t].identity.name} moves to {state.entities[recipient].identity.name}."
        )
    elif h == "use_key":
        portal_change(builder, t, True)
        p.output_lines.append("The key unlocks and opens the records door.")
    elif h == "accept_offer":
        accept_offer(builder, t, p.outcome_selection != "decline")
    elif h == "present_item":
        p.output_lines.append("The item is presented; you retain possession.")
    elif h == "social_overcome":
        assert margin is not None
        if margin < 0 and p.outcome_selection != "major_cost":
            if plan.rule_id == "gate.bluff":
                builder.emit(TriggerPayload(kind="TriggerApplied", id="gate.bluff.failed"))
                say(builder, OREN, "I need new evidence before I can admit you.", [PC])
            elif plan.rule_id == "gate.exit":
                start_conflict(builder, OREN)
                say(builder, OREN, "Then you will remain in custody.", [PC])
            else:
                say(builder, OREN, "That does not settle my concern.", [PC])
        else:
            portal_change(builder, "portal:gate", True)
            if plan.rule_id == "gate.bluff":
                change_belief(builder, "believes")
                if margin == 0:
                    id = builder.allocate("schedule")
                    builder.emit(
                        SchedulePayload(
                            kind="ScheduledEffectAdded",
                            id=id,
                            before=None,
                            after=ScheduledEffect(
                                id=id,
                                due_tick=state.clock.tick + plan.time_cost + 2,
                                handler_id="verify_invitation",
                                args=EffectArguments(actor_id=OREN, portal_id="portal:gate"),
                            ),
                        )
                    )
                say(builder, OREN, "The seal looks proper. Go through.", [PC])
            else:
                if margin == 0:
                    add_aspect(builder, PC, "noisy_intrusion", kind="situation", controller=OREN)
                    p.output_lines.append(
                        "Minor cost: Oren publicly calls attention to your passage."
                    )
                say(builder, OREN, "Keep my name out of trouble. You may pass.", [PC])
            if margin >= 3:
                add_aspect(builder, plan.actor_id, "momentary_opening")
    elif h == "pick_lock":
        assert margin is not None
        if margin < 0:
            add_aspect(builder, plan.actor_id, "noisy_intrusion", kind="situation", controller=SEN)
            p.output_lines.append("The lock stays shut; the failed attempt makes noise.")
        else:
            portal_change(builder, t, True)
            if margin == 0:
                builder.emit(
                    MovePayload(
                        kind="ItemTransferred",
                        entity_id="item:lockpicks",
                        from_id=plan.actor_id,
                        to_id=t,
                        permission="lodge",
                    ),
                    [PC],
                )
                p.output_lines.append("The lock opens, but your lockpicks lodge in the door.")
            else:
                p.output_lines.append("Your lockpicks open the door.")
            if margin >= 3:
                add_aspect(builder, plan.actor_id, "momentary_opening")
    elif h in ("discover_advantage", "create_advantage"):
        assert margin is not None
        result = outcome_branch("create_advantage", margin, plan.advantage_kind)
        if plan.aspect_id:
            old = builder.state.aspects[plan.aspect_id]
            after = old.model_copy(deep=True)
            if result.reveal:
                after.known_by = sorted(set([*after.known_by, plan.actor_id]))
            if result.free_invokes:
                after.free_invokes[plan.actor_id] = (
                    after.free_invokes.get(plan.actor_id, 0) + result.free_invokes
                )
            if result.enemy_free_invokes:
                enemy = (
                    old.subject_id
                    if old.subject_id != plan.actor_id
                    else next(
                        (id for id in builder.state.scene.participant_ids if id != plan.actor_id),
                        OREN,
                    )
                )
                after.free_invokes[enemy] = (
                    after.free_invokes.get(enemy, 0) + result.enemy_free_invokes
                )
                after.known_by = sorted(set(after.known_by + [enemy]))
            if after != old:
                builder.emit(
                    AspectPayload(kind="AspectChanged", id=old.id, before=old, after=after),
                    after.known_by,
                )
            if result.reveal:
                p.output_lines.append(f"You discover: {after.text}.")
            elif margin < 0:
                p.output_lines.append("The concern remains undiscovered.")
        elif result.aspect:
            grounding = (
                ComponentGrounding(id=t, predicate="burning")
                if intent.method == "burning"
                else None
            )
            if intent.method == "jammed_portal":
                item = intent.presented_item_ids[0]
                builder.emit(
                    MovePayload(
                        kind="ItemTransferred",
                        entity_id=item,
                        from_id=plan.actor_id,
                        to_id=t,
                        permission="lodge",
                    ),
                    [PC],
                )
                grounding = ComponentGrounding(id=item, predicate="lodged", value_id=t)
            add_aspect(
                builder,
                t
                if intent.method in ("jammed_portal", "burning")
                or (t in state.entities and state.entities[t].actor)
                else plan.actor_id,
                intent.method or "off_balance",
                result.free_invokes,
                "situation",
                controller=plan.actor_id,
                grounding=grounding,
            )
        elif margin < 0:
            p.output_lines.append("The attempt creates no advantage.")
        if result.boost:
            add_aspect(builder, plan.actor_id, "momentary_opening")
        # Fate rule: a defender succeeding with style gains one boost, not another tie.
        if plan.opposition and plan.opposition.kind == "active" and margin <= -3:
            add_aspect(builder, t, "momentary_opening", controller=t)
    elif h == "treat_consequence":
        assert margin is not None
        if margin >= 0:
            a = state.aspects[t]
            after = a.model_copy(deep=True)
            assert after.recovery
            after.recovery.treated = True
            after.recovery.treatment_scene = state.scene.sequence
            after.recovery.treatment_session = state.session.sequence
            builder.emit(
                AspectPayload(kind="AspectChanged", id=t, before=a, after=after), a.known_by
            )
            p.output_lines.append(
                "Treatment begins recovery; it still requires a complete later boundary."
            )
            if margin == 0:
                add_aspect(builder, plan.actor_id, "off_balance", kind="situation", controller=OREN)
                p.output_lines.append("Minor cost: hurried treatment leaves you off balance.")
            if margin >= 3:
                add_aspect(builder, plan.actor_id, "momentary_opening")
        else:
            p.output_lines.append("Treatment fails; the consequence remains.")
    elif h == "speak":
        if t == OREN and intent.goal in ("need_sen", "help", "gain_entry"):
            blocker(
                possesses(state, PC, "item:royal_seal"),
                "Oren's collateral offer requires your seal.",
            )
            offer(builder, OREN, "collateral")
            say(builder, OREN, "Leave the seal with me as collateral, and I will admit you.", [PC])
        elif t == SEN and intent.goal in ("help", "key"):
            blocker(possesses(state, SEN, "item:records_key"), "Sen no longer holds the key.")
            offer(builder, SEN, "key_loan")
            say(builder, SEN, "Take my key, if you promise to return it.", [PC])
        elif t == SEN and intent.goal == "ledger":
            say(
                builder,
                SEN,
                "The ledger shows that the deputy redirected relief supplies. Please preserve it.",
                [PC],
                ["proposition:ledger_incriminates_deputy"],
            )
        elif t == SEN:
            say(builder, SEN, "Lea! I am glad you came.", [PC])
        else:
            say(builder, OREN, "I admit authorized visitors. Tell me what you need.", [PC])


def evaluate_quests(builder: EventBuilder) -> None:
    state = builder.state
    old = state.quests["recover_ledger"]
    if old.terminal_result:
        return
    after = old.model_copy(deep=True)
    if possesses(state, PC, "item:ledger") and after.stage == "active":
        after.stage = "acquired"
        after.completed_objective_ids = ["ledger_acquired"]
    if possesses(state, PC, "item:ledger") and location_of(state, PC) == "location:gatehouse":
        after.stage = "completed"
        after.terminal_result = "success"
        after.completed_objective_ids = ["ledger_acquired", "ledger_returned"]
        builder.pending.output_lines.append(
            "You carry the recovered ledger outside the gate. The mission is complete."
        )
    if after != old:
        builder.emit(QuestPayload(id=old.id, before=old, after=after), [PC])
    for commitment in list(builder.state.commitments.values()):
        if commitment.status != "active":
            continue
        f = commitment.fulfillment_predicate
        if possesses(builder.state, f.recipient_id, f.item_id):
            after_c = commitment.model_copy(update={"status": "fulfilled"})
            builder.emit(
                CommitmentPayload(
                    kind="CommitmentResolved", id=commitment.id, before=commitment, after=after_c
                ),
                [PC, commitment.creditor_id],
            )
        elif (
            f.item_id == "item:records_key"
            and location_of(builder.state, PC) == "location:gatehouse"
            and possesses(builder.state, PC, "item:ledger")
            and possesses(builder.state, PC, f.item_id)
        ):
            after_c = commitment.model_copy(update={"status": "broken"})
            builder.emit(
                CommitmentPayload(
                    kind="CommitmentResolved", id=commitment.id, before=commitment, after=after_c
                ),
                [PC],
            )
            r = builder.state.relationships["relationship:sen_lea"]
            builder.emit(
                RelationshipPayload(
                    id=r.id, before=r, after=r.model_copy(update={"value": max(-2, r.value - 1)})
                )
            )
            builder.pending.output_lines.append(
                "The borrowed key remains with you; your promise is broken and Sen's trust falls."
            )
        elif (
            f.item_id == "item:royal_seal"
            and co_located(builder.state, PC, OREN)
            and possesses(builder.state, PC, "item:ledger")
            and possesses(builder.state, OREN, f.item_id)
        ):
            # Authored return consent is represented as an accepted transfer offer.
            returned = offer(builder, OREN, "collateral")
            old_offer = returned
            terms = old_offer.terms.model_copy(deep=True)
            terms.transfers = [TransferTerm(item_id=f.item_id, from_id=OREN, to_id=PC)]
            adjusted = old_offer.model_copy(update={"terms": terms, "status": "accepted"})
            builder.emit(
                OfferPayload(
                    kind="OfferResolved", id=old_offer.id, before=old_offer, after=adjusted
                ),
                [PC, OREN],
            )
            builder.emit(
                MovePayload(
                    kind="ItemTransferred",
                    entity_id=f.item_id,
                    from_id=OREN,
                    to_id=PC,
                    permission="offer",
                ),
                [PC, OREN],
            )
            builder.emit(
                CommitmentPayload(
                    kind="CommitmentResolved",
                    id=commitment.id,
                    before=commitment,
                    after=commitment.model_copy(update={"status": "fulfilled"}),
                ),
                [PC, OREN],
            )
            builder.pending.output_lines.append("Oren returns your seal as agreed.")


def fail_quest(builder: EventBuilder, text: str) -> None:
    old = builder.state.quests["recover_ledger"]
    if old.terminal_result is None:
        builder.emit(
            QuestPayload(
                id=old.id,
                before=old,
                after=old.model_copy(update={"stage": "failed", "terminal_result": "failure"}),
            ),
            [PC],
        )
        builder.pending.output_lines.append(text)
