from world_game.domain.common import GameError, ValidationIssue
from world_game.domain.events import CheckPayload, Event, EventBatch
from world_game.domain.fate import SKILLS
from world_game.domain.state import WorldState
from world_game.engine.fate_rules import classify_margin, stress_capacity
from world_game.engine.queries import grounding_holds


def validate_causality(events: list[Event], committed_ids: set[str]) -> None:
    seen = set(committed_ids)
    for event in events:
        if event.id in seen or not set(event.cause_ids) <= seen:
            raise GameError("Invalid event causal reference or duplicate event ID.")
        seen.add(event.id)


def validate_state(state: WorldState) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    def check(condition: bool, path: str, message: str) -> None:
        if not condition:
            issues.append(ValidationIssue(path=path, message=message))

    occupied_consequences: set[str] = set()
    for key, entity in state.entities.items():
        check(key == entity.id, f"entities.{key}", "key does not match id")
        visited = {key}
        parent = entity.placement.container_id if entity.placement else None
        while parent:
            check(parent in state.entities, f"entities.{key}.placement", "missing parent")
            if parent not in state.entities:
                break
            if parent in visited:
                check(False, f"entities.{key}.placement", "containment cycle")
                break
            visited.add(parent)
            holder = state.entities[parent]
            check(
                holder.kind != "item",
                f"entities.{key}.placement",
                "item cannot contain another physical entity",
            )
            parent = holder.placement.container_id if holder.placement else None
        if entity.portal:
            check(
                len(set(entity.portal.endpoints)) == 2
                and all(
                    e in state.entities and state.entities[e].kind == "location"
                    for e in entity.portal.endpoints
                ),
                f"entities.{key}.portal.endpoints",
                "requires two different locations",
            )
            check(
                all(
                    i in state.entities and state.entities[i].item
                    for i in entity.portal.key_item_ids
                ),
                f"entities.{key}.portal.key_item_ids",
                "missing key item",
            )
            check(
                not (entity.portal.open and entity.portal.locked),
                f"entities.{key}.portal",
                "locked portal is open",
            )
        if entity.fate:
            sheet = entity.fate
            check(
                sheet.physical_stress_used <= stress_capacity(sheet, "physical")
                and sheet.mental_stress_used <= stress_capacity(sheet, "mental"),
                f"entities.{key}.fate",
                "stress exceeds capacity",
            )
            if key == "actor:lea":
                check(
                    set(sheet.skills) == set(SKILLS),
                    f"entities.{key}.fate.skills",
                    "PC requires all nine skills",
                )
            else:
                check(
                    sheet.fate_points == 0,
                    f"entities.{key}.fate.fate_points",
                    "NPC has no personal spend pool",
                )
            for slot, ref in sheet.consequence_slots.model_dump().items():
                if ref:
                    check(
                        ref not in occupied_consequences,
                        f"entities.{key}.fate.consequence_slots.{slot}",
                        "consequence is assigned twice",
                    )
                    occupied_consequences.add(ref)
                    aspect = state.aspects.get(ref)
                    check(
                        aspect is not None
                        and aspect.recovery is not None
                        and aspect.recovery.slot == slot
                        and aspect.recovery.severity
                        == {"mild": 2, "moderate": 4, "severe": 6}[slot],
                        f"entities.{key}.fate.consequence_slots.{slot}",
                        "consequence severity does not match its slot",
                    )
                    check(
                        ref in state.aspects
                        and state.aspects[ref].kind == "consequence"
                        and state.aspects[ref].subject_id == key,
                        f"entities.{key}.fate.consequence_slots.{slot}",
                        "invalid consequence reference",
                    )
    records = {
        **state.entities,
        **state.propositions,
        **state.beliefs,
        **state.relationships,
        **state.aspects,
        **state.offers,
        **state.commitments,
        **state.quests,
        **state.conversations,
    }
    for key, knowledge in state.knowledge.items():
        check(
            key in state.entities and state.entities[key].actor is not None,
            f"knowledge.{key}",
            "holder must be an actor",
        )
        check(
            all(ref in state.entities for ref in knowledge.known_entity_ids),
            f"knowledge.{key}",
            "unknown entity reference",
        )
        check(
            all(ref in records for ref in knowledge.known_record_ids),
            f"knowledge.{key}",
            "unknown record reference",
        )
    for key, belief in state.beliefs.items():
        check(
            belief.holder_id in state.knowledge and belief.proposition_id in state.propositions,
            f"beliefs.{key}",
            "invalid belief references",
        )
    for key, proposition in state.propositions.items():
        check(
            proposition.arguments.subject_id in state.entities
            and (
                proposition.arguments.object_id is None
                or proposition.arguments.object_id in state.entities
            ),
            f"propositions.{key}",
            "invalid proposition arguments",
        )
    for key, a in state.aspects.items():
        check(
            a.subject_id in state.entities
            and all(i in state.knowledge for i in a.known_by)
            and all(i in state.knowledge for i in a.free_invokes),
            f"aspects.{key}",
            "invalid aspect references",
        )
        check(
            a.scope.id in records or a.scope.id == state.scene.id or a.scope.kind == "scene",
            f"aspects.{key}.scope",
            "missing scope",
        )
        check(grounding_holds(state, a), f"aspects.{key}.grounding", "grounding is false")
        if a.kind == "boost":
            check(
                sum(a.free_invokes.values()) == 1,
                f"aspects.{key}",
                "boost requires exactly one free use",
            )
    for key, r in state.relationships.items():
        check(
            r.from_id in state.knowledge and r.to_id in state.knowledge,
            f"relationships.{key}",
            "missing character",
        )
    for key, c in state.conversations.items():
        check(
            c.location_id in state.entities
            and all(i in state.knowledge for i in c.participant_ids),
            f"conversations.{key}",
            "missing participant or location",
        )
    for key, o in state.offers.items():
        check(
            o.proposer_id in state.knowledge
            and o.recipient_id in state.knowledge
            and all(
                t.item_id in state.entities
                and t.from_id in state.entities
                and t.to_id in state.entities
                for t in o.terms.transfers
            ),
            f"offers.{key}",
            "missing offer reference",
        )
    for key, commitment in state.commitments.items():
        check(
            commitment.debtor_id in state.knowledge
            and commitment.creditor_id in state.knowledge
            and commitment.fulfillment_predicate.item_id in state.entities
            and commitment.fulfillment_predicate.recipient_id in state.entities,
            f"commitments.{key}",
            "missing commitment reference",
        )
    for key, effect in state.scheduled_effects.items():
        refs = [effect.args.actor_id, effect.args.item_id, effect.args.portal_id]
        check(
            effect.args.quest_id in state.quests
            and all(ref in state.entities for ref in refs if ref is not None),
            f"scheduled_effects.{key}",
            "missing scheduled reference",
        )
    check(
        all(a.subject_id in state.knowledge for a in state.scene.deferred_awards),
        "scene.deferred_awards",
        "missing award subject",
    )
    check(
        all(
            id in state.knowledge and id != "actor:lea"
            for id in state.session.npc_carryover_credits
        ),
        "session.npc_carryover_credits",
        "missing NPC credit holder",
    )
    if state.conflict:
        ids = [p.actor_id for p in state.conflict.participants]
        check(
            len(ids) == len(set(ids))
            and all(i in state.knowledge for i in ids)
            and state.conflict.active_actor_id in ids,
            "conflict",
            "invalid conflict participants",
        )
    return issues


def validate_batch(state: WorldState, batch: EventBatch) -> list[ValidationIssue]:
    from world_game.engine.reducer import apply_staged_events

    issues: list[ValidationIssue] = []
    if (
        batch.base_world_version != state.world_version
        or batch.result_world_version != state.world_version + 1
    ):
        return [ValidationIssue(path="batch", message="stale world version")]
    seen: set[str] = set()
    all_ids = {e.id for e in batch.events}
    for index, e in enumerate(batch.events):
        if (
            e.sequence != index
            or e.id in seen
            or any(c in all_ids and c not in seen for c in e.cause_ids)
        ):
            issues.append(
                ValidationIssue(
                    path=f"events.{index}", message="invalid event order or causal reference"
                )
            )
        seen.add(e.id)
        if isinstance(e.payload, CheckPayload):
            r = e.payload.record
            p = r.plan
            if p.skill is None or p.opposition is None or p.action is None:
                issues.append(
                    ValidationIssue(path=f"events.{index}", message="check lacks locked plan")
                )
                continue
            actor_sheet = state.entities[p.actor_id].fate
            assert actor_sheet
            effort = (
                sum(r.actor_dice)
                + actor_sheet.skills.get(p.skill, 0)
                + p.stunt_bonus
                + r.actor_bonus
            )
            opposition = p.opposition.difficulty
            if p.opposition.kind == "active":
                defender = state.entities[p.opposition.defender_id or ""].fate
                assert defender
                opposition = (
                    sum(r.defender_dice or [])
                    + defender.skills.get(p.opposition.skill, 0)
                    + r.defender_bonus
                )
            if r.margin != effort - opposition or r.result != classify_margin(effort, opposition):
                issues.append(
                    ValidationIssue(path=f"events.{index}", message="check arithmetic mismatch")
                )
            if p.stunt_bonus != 2 * len(set(p.eligible_stunts)):
                issues.append(
                    ValidationIssue(path=f"events.{index}", message="stunt modifier mismatch")
                )
            for side in ("actor", "defender"):
                paid = [
                    u.aspect_id
                    for u in r.invoked_aspect_uses
                    if u.side == side and u.payment == "paid"
                ]
                if len(paid) != len(set(paid)):
                    issues.append(
                        ValidationIssue(path=f"events.{index}", message="duplicate paid invoke")
                    )
                bonus = 2 * sum(u.side == side and u.mode == "bonus" for u in r.invoked_aspect_uses)
                if bonus != (r.actor_bonus if side == "actor" else r.defender_bonus):
                    issues.append(
                        ValidationIssue(path=f"events.{index}", message="invoke modifier mismatch")
                    )
    try:
        result = apply_staged_events(state, batch.events)
        issues.extend(validate_state(result))
        if result.clock.player_turn != batch.player_turn_after:
            issues.append(
                ValidationIssue(path="batch.player_turn_after", message="turn counter mismatch")
            )
    except (GameError, KeyError, AssertionError, ValueError) as error:
        issues.append(ValidationIssue(path="events", message=str(error)))
    return issues
