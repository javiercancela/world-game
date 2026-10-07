"""Durable synchronous coordinator; models propose, events commit."""

import time
import uuid
from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, ValidationError

from world_game.domain.common import GameError, canonical, digest
from world_game.domain.events import (
    AspectPayload,
    CheckPayload,
    CommitmentPayload,
    CreatedPayload,
    Event,
    KnowledgePayload,
    MovePayload,
    PointsPayload,
    PresentedPayload,
    PropositionPayload,
    RngPayload,
    ScenePayload,
    TimePayload,
    TriggerPayload,
    UtterancePayload,
)
from world_game.domain.fate import CheckRecord, InvokeUse, Source
from world_game.domain.intents import (
    Choice,
    ChoiceCommand,
    ChoiceKind,
    ChoiceOption,
    Clarification,
    Intent,
    IntentResult,
    PendingResolution,
    Phase,
)
from world_game.domain.proposals import (
    ModelRequest,
    ModelResult,
    ModelRole,
    NarrationResult,
    NpcProposal,
    RoleProfile,
)
from world_game.domain.state import (
    Award,
    Commitment,
    Entity,
    FactArguments,
    Fulfillment,
    Identity,
    Item,
    Placement,
    Proposition,
)
from world_game.engine.actions import (
    OREN,
    PC,
    SEN,
    Ruling,
    action_effects,
    conversation,
    evaluate_quests,
    plan_action,
    start_conflict,
)
from world_game.engine.builder import EventBuilder
from world_game.engine.choices import invoke_options, option, totals
from world_game.engine.conflicts import (
    absorb,
    advance_conflict,
    attack_outcome,
    concede,
    taken_out,
)
from world_game.engine.fate_rules import absorption_choices, classify_margin
from world_game.engine.queries import (
    PerceivedFact,
    Viewer,
    VisibleEntity,
    VisibleUtterance,
    co_located,
    location_of,
    project,
)
from world_game.engine.randomness import CounterDice, DiceSource
from world_game.engine.scenes import boundaries, expire_grounding, observe
from world_game.engine.scheduler import run_due
from world_game.models.descriptions import DescriptionAgent
from world_game.models.interfaces import ProviderError, StructuredModel
from world_game.persistence.sqlite import SQLiteRepository


class Coordinator:
    def __init__(
        self,
        repository: SQLiteRepository,
        provider: StructuredModel,
        dice: DiceSource | None = None,
        call_budget: int = 8,
        deadline: float = 120,
    ) -> None:
        self.repository = repository
        self.provider = provider
        self.describer = DescriptionAgent(provider, timeout_seconds=deadline)
        self.dice = dice or CounterDice()
        self.package = repository.package()
        self.call_budget = call_budget
        self.deadline = deadline
        self.started = time.monotonic()
        self.calls = 0

    def look(self) -> str:
        view = project(self.repository.load(), Viewer(actor_id=PC), "description")
        return self.describer.describe(view)

    def builder(self, pending: PendingResolution) -> EventBuilder:
        return EventBuilder(self.repository.load(), pending)

    def fresh(self, raw: str, actor: str = PC, phase: Phase = "interpreting") -> PendingResolution:
        state = self.repository.load()
        id = f"{state.campaign_id}:{uuid.uuid4()}"
        return PendingResolution(
            interaction_id=id,
            input_id=id,
            base_world_version=state.world_version,
            phase=phase,
            actor_id=actor,
            raw_input=raw,
            staged_rng=state.rng,
        )

    def persist(self, pending: PendingResolution) -> None:
        self.repository.save_pending(pending)

    def choice(
        self,
        builder: EventBuilder,
        kind: ChoiceKind,
        prompt: str,
        options: list[ChoiceOption],
        phase: Phase,
    ) -> None:
        p = builder.pending
        p.phase = phase
        p.current_choice = Choice(
            choice_id=f"choice:{p.interaction_id}:{p.choice_revision}",
            kind=kind,
            prompt=prompt,
            options=options,
            context_hash=digest(
                {
                    "state": digest(builder.state),
                    "rolls": p.recorded_rolls,
                    "revision": p.choice_revision,
                }
            ),
        )
        pc_sheet = builder.state.entities[PC].fate
        assert pc_sheet
        p.provisional_resource_balances = {
            PC: pc_sheet.fate_points,
            "gm": builder.state.scene.gm_fate_pool,
        }

    def call(
        self,
        p: PendingResolution,
        role: ModelRole,
        request: ModelRequest,
        response_type: type[BaseModel],
        suffix: str,
    ) -> BaseModel:
        key = "call:" + digest(
            {
                "interaction": p.interaction_id,
                "role": role,
                "suffix": suffix,
                "context": digest(request),
                "prompt": digest(
                    files("world_game").joinpath(f"models/prompts/{role}.txt").read_text()
                ),
                "schema": digest(response_type.model_json_schema()),
                "provider": self.provider.cache_identity(role),
            }
        )
        cached = self.repository.cached_call(key)
        if cached:
            record = ModelResult.model_validate_json(cached)
            if record.status == "ok" and record.context_hash == digest(request):
                return response_type.model_validate_json(record.payload_json)
        for attempt in range(2):
            if (
                p.provider_attempts >= self.call_budget
                or p.provider_elapsed_ms >= self.deadline * 1000
            ):
                raise ProviderError("Model call budget/deadline exhausted; use /retry or /quit.")
            self.calls += 1
            p.provider_attempts += 1
            if self.repository.committed(p.input_id) is None:
                self.persist(p)
            started = time.monotonic()
            try:
                record = self.provider.generate(
                    role=role,
                    request=request,
                    response_type=response_type,
                    call_key=key,
                    timeout_seconds=self.deadline - p.provider_elapsed_ms / 1000,
                )
                payload = response_type.model_validate_json(record.payload_json)
            except (ProviderError, ValidationError, KeyboardInterrupt) as error:
                failed = ModelResult(
                    payload_json="{}",
                    provider="unknown",
                    model="unknown",
                    status="failed",
                    context_hash=digest(request),
                    role=role,
                    request_json=canonical(request),
                )
                self.repository.record_call(key + f":attempt:{attempt}", failed)
                if isinstance(error, ProviderError) and error.retryable and attempt == 0:
                    continue
                raise
            finally:
                p.provider_elapsed_ms += int((time.monotonic() - started) * 1000)
                if self.repository.committed(p.input_id) is None:
                    self.persist(p)
            break
        record.role = role
        record.request_json = canonical(request)
        record.context_hash = digest(request)
        self.repository.record_call(key, record)
        if key not in p.completed_model_call_keys:
            p.completed_model_call_keys.append(key)
        return payload

    def submit(self, raw: str) -> str:
        self.started = time.monotonic()
        self.calls = 0
        p = self.repository.pending()
        if p is not None:
            if p.current_choice:
                exact = [
                    o
                    for o in p.current_choice.options
                    if raw.casefold().strip() in (o.id.casefold(), o.label.casefold())
                ]
                if len(exact) == 1:
                    return self.choose(exact[0].id)
            return self.describe(p)
        boundary = self.ensure_boundary()
        if boundary:
            return self.describe(boundary)
        p = self.fresh(raw)
        self.persist(p)
        return self.resume(p)

    def resume(self, p: PendingResolution | None = None) -> str:
        p = p or self.repository.pending()
        if p is None:
            return "No pending interaction."
        self.started = time.monotonic()
        self.calls = 0
        try:
            if p.phase == "failed":
                # Explicit /retry begins a new bounded attempt, retaining successful calls and dice.
                p.provider_attempts = 0
                p.provider_elapsed_ms = 0
                p.phase = p.resume_phase or "interpreting"
                p.resume_phase = None
                self.persist(p)
            if p.phase == "interpreting":
                view = project(
                    self.repository.load(),
                    Viewer(actor_id=PC),
                    "interpreter",
                    p.choice_revision,
                    self.repository.utterances(PC),
                )
                req = ModelRequest(context=view, text=p.raw_input)
                result = self.call(p, "interpreter", req, IntentResult, str(p.choice_revision))
                assert isinstance(result, IntentResult)
                payload = result.payload
                if isinstance(payload, Clarification):
                    valid = {e.id for e in view.visible_entities}
                    candidates = [i for i in payload.candidates if i in valid]
                    b = self.builder(p)
                    self.choice(
                        b,
                        "clarification",
                        payload.prompt,
                        [
                            option(
                                i,
                                self.repository.load().entities[i].identity.name,
                                "clarify",
                                target_id=i,
                            )
                            for i in candidates
                        ],
                        "awaiting_clarification",
                    )
                    self.persist(p)
                    return self.describe(p)
                allowed = {e.id for e in view.visible_entities} | {a.id for a in view.known_aspects}
                if any(
                    ref not in allowed
                    for ref in payload.target_ids
                    + payload.presented_item_ids
                    + ([payload.adjacent_move_id] if payload.adjacent_move_id else [])
                ) or any(
                    ref not in {f.id for f in view.perceived_facts} for ref in payload.claim_refs
                ):
                    repair = req.model_copy(
                        update={
                            "repair": "Use only supplied visible references and known claim IDs, or return a clarification. No state mutations are allowed."
                        }
                    )
                    repaired = self.call(
                        p, "interpreter", repair, IntentResult, str(p.choice_revision) + ":repair"
                    )
                    assert isinstance(repaired, IntentResult)
                    payload = repaired.payload
                    if isinstance(payload, Clarification):
                        self.choice(
                            self.builder(p),
                            "clarification",
                            payload.prompt,
                            [],
                            "awaiting_clarification",
                        )
                        self.persist(p)
                        return self.describe(p)
                    if any(
                        ref not in allowed
                        for ref in payload.target_ids
                        + payload.presented_item_ids
                        + ([payload.adjacent_move_id] if payload.adjacent_move_id else [])
                    ) or any(
                        ref not in {f.id for f in view.perceived_facts}
                        for ref in payload.claim_refs
                    ):
                        raise ProviderError(
                            "Interpreter proposed unavailable references after repair; use /retry or /cancel."
                        )
                p.interpreted_intent = payload
                p.phase = "planning"
                self.persist(p)
            if p.phase == "planning":
                assert p.interpreted_intent
                b = self.builder(p)
                intent = p.interpreted_intent
                if intent.adjacent_move_id:
                    if b.state.conflict is None:
                        raise GameError(
                            "An extra adjacent move is available only alongside a conflict action."
                        )
                    move_intent = Intent(handler_id="move", target_ids=[intent.adjacent_move_id])
                    plan_action(b.state, move_intent, Ruling(rules=self.package.rules), p.actor_id)
                    origin = location_of(b.state, p.actor_id)
                    portal = next(
                        e
                        for e in b.state.entities.values()
                        if e.portal and set(e.portal.endpoints) == {origin, intent.adjacent_move_id}
                    )
                    assert origin
                    b.emit(
                        MovePayload(
                            kind="EntityMoved",
                            entity_id=p.actor_id,
                            from_id=origin,
                            to_id=intent.adjacent_move_id,
                            permission="traverse",
                            portal_id=portal.id,
                        ),
                        [PC],
                    )
                # Check legality before recording speech or establishing a conversation.
                plan_action(b.state, intent, Ruling(rules=self.package.rules), p.actor_id)
                if (
                    intent.target_ids
                    and intent.target_ids[0] in b.state.knowledge
                    and intent.handler_id != "attack"
                ):
                    conversation(b, p.actor_id, intent.target_ids[0], intent.goal)
                p.resolution_plan = plan_action(
                    b.state, intent, Ruling(rules=self.package.rules), p.actor_id
                )
                b.rule_id = p.resolution_plan.rule_id
                if intent.new_claim:
                    claim_id = b.allocate("proposition")
                    claim = Proposition(
                        id=claim_id,
                        predicate="claim",
                        arguments=FactArguments(
                            subject_id=p.actor_id,
                            object_id=intent.target_ids[0]
                            if intent.target_ids and intent.target_ids[0] in b.state.entities
                            else None,
                            value=intent.new_claim,
                        ),
                        truth="unknown",
                        source=Source(
                            kind="event", id=f"event:{p.input_id}:{len(p.staged_events)}"
                        ),
                        discovery_policy_id="heard_claim",
                    )
                    b.emit(PropositionPayload(record=claim))
                    before_knowledge = b.state.knowledge[p.actor_id]
                    after_knowledge = before_knowledge.model_copy(deep=True)
                    after_knowledge.known_record_ids = sorted(
                        set(after_knowledge.known_record_ids + [claim_id])
                    )
                    b.emit(
                        KnowledgePayload(
                            holder_id=p.actor_id, before=before_knowledge, after=after_knowledge
                        ),
                        [p.actor_id],
                    )
                    intent.claim_refs = sorted(set(intent.claim_refs + [claim_id]))
                if (
                    intent.speech
                    and intent.target_ids
                    and intent.target_ids[0] in b.state.knowledge
                ):
                    claims = intent.claim_refs or (
                        ["proposition:lea_expected"]
                        if p.resolution_plan.rule_id == "gate.bluff"
                        else []
                    )
                    b.emit(
                        UtterancePayload(
                            speaker_id=p.actor_id,
                            text=intent.speech,
                            audience_ids=[intent.target_ids[0]],
                            claims=claims,
                        ),
                        [PC, intent.target_ids[0]],
                    )
                for item in intent.presented_item_ids:
                    audience = (
                        [intent.target_ids[0]]
                        if intent.target_ids and intent.target_ids[0] in b.state.knowledge
                        else []
                    )
                    b.emit(
                        PresentedPayload(actor_id=p.actor_id, item_id=item, audience_ids=audience),
                        [PC, *audience],
                    )
                if intent.handler_id == "attack":
                    start_conflict(b, intent.target_ids[0] if p.actor_id == PC else p.actor_id)
                p.phase = "rolling" if p.resolution_plan.action else "resolving_outcome"
                self.persist(p)
            if p.phase == "rolling":
                self.roll(p)
                b = self.builder(p)
                self.choice(
                    b,
                    "invoke",
                    "Invoke a known relevant aspect, or /pass to accept this result.",
                    [option("pass", "Accept the current result", "pass"), *invoke_options(b, PC)],
                    "awaiting_invoke",
                )
                self.persist(p)
                return self.describe(p)
            if p.phase in (
                "awaiting_invoke",
                "awaiting_outcome_choice",
                "awaiting_pre_roll_choice",
                "awaiting_clarification",
            ):
                return self.describe(p)
            return self.finish(p)
        except (ProviderError, ValidationError, KeyboardInterrupt) as error:
            p.resume_phase = p.phase
            p.phase = "failed"
            self.persist(p)
            return (
                "Provider interrupted; pending input preserved. Use /retry or /quit."
                if isinstance(error, KeyboardInterrupt)
                else f"{error}\nPending input preserved; use /retry or /quit."
            )
        except GameError as error:
            if not p.recorded_rolls and not p.completed_choices:
                # Impossible actions have no canonical cost; discard operational speech only.
                p.staged_events.clear()
                self.repository.cancel(p)
                return str(error)
            p.resume_phase = p.phase
            p.phase = "failed"
            self.persist(p)
            raise

    def roll(self, p: PendingResolution, side: Literal["actor", "defender"] | None = None) -> None:
        plan = p.resolution_plan
        assert plan and plan.opposition
        sides: tuple[Literal["actor", "defender"], ...] = (
            (side,)
            if side
            else ("actor", "defender")
            if plan.opposition.kind == "active"
            else ("actor",)
        )
        for s in sides:
            if side is None and any(d.side == s for d in p.recorded_rolls):
                continue
            index = sum(d.side == s for d in p.recorded_rolls)
            draw, p.staged_rng = self.dice.roll(p.staged_rng, f"check:{p.input_id}", s, index)
            p.recorded_rolls.append(draw)
            if s == "actor":
                p.actor_dice = draw.dice
            else:
                p.defender_dice = draw.dice
        # Persist before showing any dice or making any dependent provider call.

    def spend(self, b: EventBuilder, command: ChoiceCommand, actor_id: str) -> None:
        p = b.pending
        plan = p.resolution_plan
        assert plan and command.aspect_id
        legal = invoke_options(b, actor_id)
        if not any(o.command == command for o in legal):
            raise GameError("That invoke is unavailable.")
        a = b.state.aspects[command.aspect_id]
        side: Literal["actor", "defender"] = "actor" if actor_id == plan.actor_id else "defender"
        if command.payment == "free":
            after = a.model_copy(deep=True)
            after.free_invokes[actor_id] -= 1
            if a.kind == "boost":
                b.emit(
                    AspectPayload(
                        kind="AspectRemoved", id=a.id, before=a, after=None, reason="boost spent"
                    ),
                    a.known_by,
                )
            else:
                b.emit(
                    AspectPayload(kind="AspectChanged", id=a.id, before=a, after=after), a.known_by
                )
        else:
            sheet = b.state.entities[actor_id].fate
            assert sheet
            balance = sheet.fate_points if actor_id == PC else b.state.scene.gm_fate_pool
            b.emit(
                PointsPayload(
                    account=actor_id if actor_id == PC else "gm",
                    before=balance,
                    after=balance - 1,
                    reason="invoke",
                ),
                [PC],
            )
            if a.subject_id in b.state.knowledge and a.subject_id != actor_id:
                scene = b.state.scene
                after_scene = scene.model_copy(deep=True)
                after_scene.deferred_awards.append(Award(subject_id=a.subject_id, amount=1))
                b.emit(
                    ScenePayload(
                        kind="SceneChanged",
                        before=scene,
                        after=after_scene,
                        transition_id="fate.hostile_invoke",
                    )
                )
        p.invoke_ledger.append(
            InvokeUse(
                aspect_id=a.id,
                controller_id=actor_id,
                side=side,
                payment=command.payment,
                mode=command.mode,
            )
        )
        if command.mode == "reroll":
            self.roll(p, side)
        elif side == "actor":
            p.actor_bonus += 2
        else:
            p.defender_bonus += 2
        p.pass_count = 0
        p.output_lines.append(
            f"{b.state.entities[actor_id].identity.name} invokes {a.text if PC in a.known_by else 'an undisclosed aspect'} for {'+2' if command.mode == 'bonus' else 'a reroll'} ({command.payment})."
        )

    def npc_response(self, b: EventBuilder) -> bool:
        p = b.pending
        plan = p.resolution_plan
        assert plan and plan.opposition
        if plan.opposition.kind != "active":
            return False
        npc = plan.opposition.defender_id if plan.actor_id == PC else plan.actor_id
        assert npc
        side = "defender" if plan.actor_id == PC else "actor"
        _, _, margin = totals(b)
        npc_margin = -margin if side == "defender" else margin
        if classify_margin(npc_margin + 2, 0) == classify_margin(npc_margin, 0):
            return False
        options = sorted(
            invoke_options(b, npc),
            key=lambda o: (o.command.payment != "free", o.command.aspect_id or ""),
        )
        if not options:
            return False
        self.spend(b, options[0].command, npc)
        return True

    def choose(self, option_id: str, response_id: str | None = None) -> str:
        response_id = response_id or str(uuid.uuid4())
        if self.repository.response(response_id):
            return "Choice already accepted; no resources changed."
        p = self.repository.pending()
        if p is None or p.current_choice is None:
            raise GameError("No choice is pending.")
        selected = next((o for o in p.current_choice.options if o.id == option_id), None)
        if (
            selected is None
            and option_id.isdigit()
            and 1 <= int(option_id) <= len(p.current_choice.options)
        ):
            selected = p.current_choice.options[int(option_id) - 1]
        if selected is None:
            raise GameError("Select a current option ID or its number.")
        revision = p.choice_revision
        p = p.model_copy(deep=True)
        b = self.builder(p)
        command = selected.command
        current_kind = p.current_choice.kind  # type: ignore[union-attr]
        p.choice_revision += 1
        p.completed_choices.append(p.current_choice.choice_id)  # type: ignore[union-attr]
        p.current_choice = None
        if current_kind == "invoke":
            if command.kind == "invoke":
                self.spend(b, command, PC)
            else:
                p.pass_count += 1
            npc_spent = self.npc_response(b)
            if command.kind == "pass" and not npc_spent:
                p.pass_count = 2
                p.phase = "resolving_outcome"
            else:
                self.choice(
                    b,
                    "invoke",
                    "Response opportunity: invoke again or /pass.",
                    [option("pass", "Accept the current result", "pass"), *invoke_options(b, PC)],
                    "awaiting_invoke",
                )
        elif current_kind == "offer":
            assert p.interpreted_intent
            p.outcome_selection = "decline" if command.kind == "decline" else "accept"
            p.phase = "resolving_outcome"
        elif current_kind == "compel":
            self.resolve_compel(b, command)
            p.effects_applied = True
            p.phase = "staging_reactions"
        elif current_kind == "harm":
            assert p.defender_id and p.resolution_plan
            if command.kind == "taken_out":
                taken_out(b, p.defender_id)
            else:
                absorb(
                    b,
                    p.defender_id,
                    p.resolution_plan.actor_id,
                    p.damage,
                    command.stress,
                    command.slots,
                    self.package.rules.consequence_catalog,
                )
            p.outcome_applied = True
            p.phase = "resolving_outcome"
        elif current_kind == "cost":
            p.outcome_selection = command.kind
            p.phase = "resolving_outcome"
        elif current_kind == "pre_roll":
            if command.kind == "concede":
                self.concession_choice(b)
            else:
                assert p.resolution_plan
                if p.resolution_plan.opposition is None:
                    p.phase = "resolving_outcome"
                else:
                    p.resolution_plan.opposition.skill = (
                        "fight" if selected.id == "continue:fight" else "athletics"
                    )
                    p.phase = "rolling"
        elif current_kind == "concession":
            assert command.terms
            concede(b, command.terms)
            p.effects_applied = True
            p.outcome_applied = True
            p.phase = "staging_reactions"
        elif current_kind == "next_actor":
            advance_conflict(b, command.target_id)
            p.conflict_order_applied = True
            p.phase = "staging_reactions"
        elif current_kind == "clarification":
            assert command.target_id
            p.raw_input += " " + b.state.entities[command.target_id].identity.name
            p.phase = "interpreting"
        self.repository.save_pending(
            p, expected_revision=revision, response_id=response_id, option_id=selected.id
        )
        return self.resume(p)

    def stage_check(self, b: EventBuilder) -> int:
        p = b.pending
        if not p.check_staged:
            plan = p.resolution_plan
            assert plan and p.actor_dice
            if b.state.rng != p.staged_rng:
                b.emit(RngPayload(before=b.state.rng, after=p.staged_rng))
            _, _, margin = totals(b)
            record = CheckRecord(
                id=f"check:{p.input_id}",
                plan=plan,
                actor_dice=p.actor_dice,
                defender_dice=p.defender_dice,
                actor_bonus=p.actor_bonus,
                defender_bonus=p.defender_bonus,
                invoked_aspect_uses=p.invoke_ledger,
                margin=margin,
                result=classify_margin(margin, 0),
                rng_continuation=p.staged_rng,
                draws=p.recorded_rolls,
            )
            b.emit(CheckPayload(record=record), [PC])
            p.output_lines.append(self.mechanics(b, final=True))
            p.check_staged = True
        return totals(b)[2]

    def finish(self, p: PendingResolution) -> str:
        b = self.builder(p)
        plan = p.resolution_plan
        if not p.effects_applied:
            assert plan
            margin = self.stage_check(b) if plan.action else None
            if plan.handler_id == "attack" and not p.outcome_applied:
                assert margin is not None
                if margin >= 3 and p.outcome_selection is None:
                    self.choice(
                        b,
                        "cost",
                        "Trade one shift of damage for a boost?",
                        [
                            option("keep", "Keep all damage", "keep"),
                            option("trade", "Trade one shift for a boost", "trade"),
                        ],
                        "awaiting_outcome_choice",
                    )
                    self.persist(p)
                    return self.describe(p)
                damage = attack_outcome(b, margin, p.outcome_selection == "trade")
                p.damage = damage
                p.defender_id = plan.target_id
                if damage:
                    defender = b.state.entities[plan.target_id or ""].fate
                    assert defender and plan.target_id
                    allocations = absorption_choices(defender, plan.harm_type, damage)
                    if plan.target_id == PC:
                        options = [
                            option(
                                f"absorb:{index}",
                                f"Use {a.stress} stress; consequences: {', '.join(a.slots) or 'none'}",
                                "absorb",
                                stress=a.stress,
                                slots=a.slots,
                            )
                            for index, a in enumerate(allocations)
                        ]
                        options.append(option("taken_out", "Accept capture", "taken_out"))
                        self.choice(
                            b,
                            "harm",
                            f"Absorb all {damage} shifts, or be taken out.",
                            options,
                            "awaiting_outcome_choice",
                        )
                        self.persist(p)
                        return self.describe(p)
                    if allocations:
                        a = allocations[0]
                        absorb(
                            b,
                            plan.target_id,
                            plan.actor_id,
                            damage,
                            a.stress,
                            a.slots,
                            self.package.rules.consequence_catalog,
                        )
                    else:
                        taken_out(b, plan.target_id)
                p.outcome_applied = True
            elif plan.handler_id != "attack":
                if (
                    margin is not None
                    and margin < 0
                    and plan.major_cost_template_id
                    and p.outcome_selection is None
                ):
                    self.choice(
                        b,
                        "cost",
                        "Accept the authored major cost for success?",
                        [
                            option("failure", "Accept failure", "failure"),
                            option("major_cost", "Accept major-cost success", "major_cost"),
                        ],
                        "awaiting_outcome_choice",
                    )
                    self.persist(p)
                    return self.describe(p)
                action_effects(b, margin)
            p.effects_applied = True
            p.phase = "staging_reactions"
            self.persist(p)
        if p.phase == "staging_reactions":
            self.voice_npcs(b)
            if not p.time_applied:
                if b.state.conflict:
                    candidates = [] if p.conflict_order_applied else advance_conflict(b)
                    if candidates:
                        p.next_actor_candidates = candidates
                        self.choice(
                            b,
                            "next_actor",
                            "Choose the next exchange's first actor.",
                            [
                                option(
                                    id,
                                    b.state.entities[id].identity.name,
                                    "next_actor",
                                    target_id=id,
                                )
                                for id in candidates
                            ],
                            "awaiting_outcome_choice",
                        )
                        self.persist(p)
                        return self.describe(p)
                else:
                    before = b.state.clock
                    # Ending a conflict already charges its final partial exchange.
                    was_conflict = (
                        any(e.type in ("ConflictStarted", "ConflictEnded") for e in p.staged_events)
                        or b.base.conflict is not None
                    )
                    tick = 0 if was_conflict else plan.time_cost if plan else 0
                    turn = (
                        1
                        if p.actor_id == PC and p.raw_input not in ("authored compel", "concession")
                        else 0
                    )
                    b.emit(
                        TimePayload(
                            before=before,
                            after=before.model_copy(
                                update={
                                    "tick": before.tick + tick,
                                    "player_turn": before.player_turn + turn,
                                }
                            ),
                        )
                    )
                if b.state.conflict and p.actor_id == PC:
                    before = b.state.clock
                    b.emit(
                        TimePayload(
                            before=before,
                            after=before.model_copy(update={"player_turn": before.player_turn + 1}),
                        )
                    )
                p.time_applied = True
                run_due(b)
                evaluate_quests(b)
                expire_grounding(b)
                boundaries(b)
                observe(b)
            p.phase = "validating"
            self.persist(p)
        batch = b.batch()
        fallback = "\n".join(p.output_lines) or "The action is committed."
        self.repository.commit(batch, p, fallback)
        # Committed outcomes remain authoritative even if narration or terminal I/O fails.
        narration = self.narrate(p, batch.events, fallback)
        text = self.repository.present(batch.batch_id, narration)
        return text

    def voice_npcs(self, b: EventBuilder) -> None:
        p = b.pending
        events = [
            e
            for e in p.staged_events
            if isinstance(e.payload, UtterancePayload)
            and e.payload.speaker_id != PC
            and PC in e.payload.audience_ids
        ]
        for e in events[:2]:
            payload = e.payload
            assert isinstance(payload, UtterancePayload)
            suffix = e.id
            if e.id in p.approved_npc_event_ids:
                continue
            view = project(
                b.state,
                Viewer(actor_id=payload.speaker_id),
                "npc",
                p.choice_revision,
                self.repository.utterances(payload.speaker_id),
            )
            heard = []
            for event in p.staged_events:
                speech = event.payload
                if (
                    isinstance(speech, UtterancePayload)
                    and speech.speaker_id == PC
                    and payload.speaker_id in speech.audience_ids
                ):
                    heard.append(speech.text)
                    view.recent_utterances.append(
                        VisibleUtterance(id=event.id, speaker_id=PC, text=speech.text)
                    )
                if (
                    isinstance(speech, PresentedPayload)
                    and payload.speaker_id in speech.audience_ids
                ):
                    item = b.state.entities[speech.item_id]
                    view.visible_entities.append(
                        VisibleEntity(
                            id=item.id,
                            name=item.identity.name,
                            aliases=item.identity.aliases,
                            description=item.identity.description,
                            kind="item",
                        )
                    )
                    view.perceived_facts.append(
                        PerceivedFact(
                            id=item.id,
                            description="presented",
                            value=item.identity.name,
                            source=event.id,
                            acquired_tick=event.tick,
                        )
                    )
            view.recent_utterances = view.recent_utterances[-12:]
            profile = next(
                profile
                for profile in self.package.profiles
                if profile.actor_id == payload.speaker_id
            )
            own_actor = b.state.entities[payload.speaker_id].actor
            assert own_actor
            request = ModelRequest(
                context=view,
                role_profile=RoleProfile(
                    actor_id=payload.speaker_id,
                    public=profile.public,
                    private=profile.private,
                    goals=own_actor.goal_ids,
                ),
                text="\n".join(heard),
                permitted_responses=[e.rule_id],
                evidence_refs=payload.disclosures,
                fallback=payload.text,
            )
            result = self.call(p, "npc", request, NpcProposal, suffix)
            assert isinstance(result, NpcProposal)
            if (
                result.selected_response != e.rule_id
                or not set(result.disclosure_refs + result.claim_refs) <= set(request.evidence_refs)
                or result.offer_template_id is not None
            ):
                repaired = self.call(
                    p,
                    "npc",
                    request.model_copy(
                        update={
                            "repair": "Keep the permitted response, disclose only supplied evidence, and use no new offer. Return the fallback if uncertain."
                        }
                    ),
                    NpcProposal,
                    suffix + ":repair",
                )
                assert isinstance(repaired, NpcProposal)
                result = repaired
                if (
                    result.selected_response != e.rule_id
                    or not set(result.disclosure_refs + result.claim_refs)
                    <= set(request.evidence_refs)
                    or result.offer_template_id is not None
                ):
                    raise ProviderError(
                        "NPC proposal was incompatible with the settled response after repair."
                    )
            old_line = f"{b.state.entities[payload.speaker_id].identity.name}: {payload.text}"
            new_line = f"{b.state.entities[payload.speaker_id].identity.name}: {result.utterance}"
            for index, line in enumerate(p.output_lines):
                if line == old_line:
                    p.output_lines[index] = new_line
            replacement = e.model_copy(deep=True)
            assert isinstance(replacement.payload, UtterancePayload)
            replacement.payload.text = result.utterance
            p.staged_events[e.sequence] = replacement
            p.approved_npc_event_ids.append(e.id)
            self.persist(p)

    def narrate(self, p: PendingResolution, events: list[Event], fallback: str) -> str | None:
        try:
            view = project(
                self.repository.load(),
                Viewer(actor_id=PC),
                "narrator",
                utterances=self.repository.utterances(PC),
            )
            visible_events = [e.id for e in events if PC in e.observed_by]
            request = ModelRequest(
                context=view,
                text="Present the committed outcome.",
                evidence_refs=visible_events,
                fallback=fallback,
            )
            result = self.call(p, "narrator", request, NarrationResult, "committed")
            assert isinstance(result, NarrationResult)
            if not set(result.used_event_ids) <= set(visible_events) or not set(
                result.mentioned_entity_ids
            ) <= {e.id for e in view.visible_entities}:
                return None
            # Preserve observable mechanics and approved speech. Extra stylistic prose
            # is not canonical; factual consistency remains a live-evaluation concern.
            return result.prose if fallback in result.prose else None
        except (ProviderError, ValidationError, KeyboardInterrupt):
            return None

    def mechanics(self, b: EventBuilder, final: bool = False) -> str:
        p = b.pending
        plan = p.resolution_plan
        assert plan
        effort, opposition, margin = totals(b)
        return (
            f"{'Final' if final else 'Provisional'}: {plan.action} / {plan.skill}; dice {p.actor_dice}; stunt +{plan.stunt_bonus}, invokes +{p.actor_bonus}; effort {effort} vs {opposition}"
            + (
                f" (defense dice {p.defender_dice}, invokes +{p.defender_bonus})"
                if p.defender_dice
                else ""
            )
            + f"; margin {margin}: {classify_margin(margin, 0)}."
        )

    def describe(self, p: PendingResolution | None = None) -> str:
        p = p or self.repository.pending()
        if not p:
            return "No pending interaction."
        b = self.builder(p)
        lines = ["Pending interaction (provisional; world effects are not committed)."]
        if p.resolution_plan:
            lines.append(f"Goal: {p.resolution_plan.goal}. Stakes: {p.resolution_plan.stakes}")
        if p.actor_dice:
            lines.append(self.mechanics(b))
        sheet = b.state.entities[PC].fate
        assert sheet
        lines.append(f"Fate points: {sheet.fate_points}; GM pool: {b.state.scene.gm_fate_pool}.")
        if p.current_choice:
            lines.append(p.current_choice.prompt)
            lines.extend(
                f"{i}. {o.label} [{o.id}]" for i, o in enumerate(p.current_choice.options, 1)
            )
        elif p.phase == "failed":
            lines.append("Technical failure. /retry resumes this input; /quit saves it.")
        return "\n".join(lines)

    def ensure_boundary(self) -> PendingResolution | None:
        existing = self.repository.pending()
        if existing:
            return existing
        state = self.repository.load()
        if state.quests["recover_ledger"].terminal_result:
            return None
        offers = sorted(
            (
                o
                for o in state.offers.values()
                if o.status == "pending"
                and o.expires_tick >= state.clock.tick
                and co_located(state, o.proposer_id, PC)
            ),
            key=lambda o: o.id,
        )
        if offers:
            o = offers[0]
            p = self.fresh("accept offer", phase="awaiting_outcome_choice")
            p.interpreted_intent = Intent(handler_id="accept_offer", target_ids=[o.id])
            p.resolution_plan = plan_action(
                state, p.interpreted_intent, Ruling(rules=self.package.rules)
            )
            b = self.builder(p)
            self.choice(
                b,
                "offer",
                "Accept the proposed transfer and obligation?",
                [
                    option("accept", "Accept the offer", "accept"),
                    option("decline", "Decline the offer", "decline"),
                ],
                "awaiting_outcome_choice",
            )
            self.persist(p)
            return p
        if "compel:debt:offered" not in state.applied_trigger_ids and any(
            c.status == "active"
            and c.fulfillment_predicate.item_id == "item:records_key"
            and c.debtor_id == PC
            for c in state.commitments.values()
        ):
            p = self.fresh("authored compel", phase="awaiting_outcome_choice")
            b = self.builder(p)
            options = [
                option("accept", "Deliver Sen's receipt to Oren; gain one Fate point", "accept"),
                option(
                    "object", "Object: this compel does not fit; withdraw without charge", "object"
                ),
            ]
            sheet = state.entities[PC].fate
            assert sheet
            if sheet.fate_points:
                options.insert(
                    1, option("refuse", "Refuse the complication; spend one Fate point", "refuse")
                )
            self.choice(
                b,
                "compel",
                "Compel: Cannot Leave a Debt Unpaid. Sen needs a receipt delivered to Oren before you leave.",
                options,
                "awaiting_outcome_choice",
            )
            self.persist(p)
            return p
        if state.conflict and state.conflict.active_actor_id != PC:
            npc = state.conflict.active_actor_id
            p = self.fresh(
                f"{state.entities[npc].identity.name} attacks to capture you.",
                npc,
                "awaiting_pre_roll_choice",
            )
            b = self.builder(p)
            npc_location = location_of(b.state, npc)
            pc_location = location_of(b.state, PC)
            if npc_location != pc_location:
                portals = sorted(
                    (
                        e
                        for e in b.state.entities.values()
                        if e.portal
                        and npc_location in e.portal.endpoints
                        and e.portal.open
                        and not e.portal.locked
                    ),
                    key=lambda e: e.id,
                )
                direct = next(
                    (e for e in portals if e.portal and pc_location in e.portal.endpoints), None
                )
                passage = None
                candidates = ([direct] if direct else []) + [e for e in portals if e != direct]
                for candidate in candidates:
                    assert candidate.portal
                    destination = next(
                        id for id in candidate.portal.endpoints if id != npc_location
                    )
                    try:
                        plan_action(
                            b.state,
                            Intent(handler_id="move", target_ids=[destination]),
                            Ruling(rules=self.package.rules),
                            npc,
                        )
                    except GameError:
                        continue
                    passage = candidate
                    break
                if passage and passage.portal:
                    destination = next(id for id in passage.portal.endpoints if id != npc_location)
                    assert npc_location
                    b.emit(
                        MovePayload(
                            kind="EntityMoved",
                            entity_id=npc,
                            from_id=npc_location,
                            to_id=destination,
                            permission="traverse",
                            portal_id=passage.id,
                        ),
                        [PC],
                    )
                    p.output_lines.append(
                        f"{b.state.entities[npc].identity.name} moves to {b.state.entities[destination].identity.name}."
                    )
            close = co_located(b.state, npc, PC)
            p.interpreted_intent = Intent(
                handler_id="attack" if close else "wait",
                target_ids=[PC] if close else [],
                goal="fight" if close else "pursue",
            )
            p.resolution_plan = plan_action(
                b.state, p.interpreted_intent, Ruling(rules=self.package.rules), npc
            )
            self.choice(
                b,
                "pre_roll",
                f"{b.state.entities[npc].identity.name} closes in to restrain you. Continue or concede before dice.",
                (
                    [
                        option("continue:athletics", "Continue; defend with Athletics", "continue"),
                        option("continue:fight", "Continue; defend with Fight", "continue"),
                    ]
                    if close
                    else [option("continue", "Continue the pursuit action", "continue")]
                )
                + [option("concede", "Concede before the roll", "concede")],
                "awaiting_pre_roll_choice",
            )
            self.persist(p)
            return p
        return None

    def resolve_compel(self, b: EventBuilder, command: ChoiceCommand) -> None:
        b.emit(TriggerPayload(kind="TriggerApplied", id="compel:debt:offered"))
        sheet = b.state.entities[PC].fate
        assert sheet
        if command.kind == "accept":
            b.emit(
                PointsPayload(
                    account=PC,
                    before=sheet.fate_points,
                    after=sheet.fate_points + 1,
                    reason="compel",
                ),
                [PC],
            )
            b.emit(
                CreatedPayload(
                    record=Entity(
                        id="item:archive_receipt",
                        kind="item",
                        identity=Identity(
                            name="Archive Receipt",
                            aliases=["receipt"],
                            description="Sen's receipt for Oren.",
                        ),
                        placement=Placement(container_id=PC),
                        item=Item(tags=["receipt"]),
                    )
                ),
                [PC, SEN],
            )
            id = b.allocate("commitment")
            obligation = Commitment(
                id=id,
                debtor_id=PC,
                creditor_id=SEN,
                fulfillment_predicate=Fulfillment(
                    kind="delivered", item_id="item:archive_receipt", recipient_id=OREN
                ),
                breach_trigger_id="receipt_before_exit",
                source_event_id=f"event:{b.pending.input_id}:{len(b.pending.staged_events)}",
            )
            b.emit(
                CommitmentPayload(kind="CommitmentMade", id=id, before=None, after=obligation),
                [PC, SEN],
            )
            b.pending.output_lines.append(
                "Compel accepted: gain one Fate point and carry Sen's receipt for Oren."
            )
        elif command.kind == "refuse":
            b.emit(
                PointsPayload(
                    account=PC,
                    before=sheet.fate_points,
                    after=sheet.fate_points - 1,
                    reason="refusal",
                ),
                [PC],
            )
            b.pending.output_lines.append("Compel refused: spend one Fate point.")
        else:
            b.pending.output_lines.append(
                "Compel objected to and withdrawn without charge; objection recorded."
            )

    def concession_choice(self, b: EventBuilder) -> None:
        if b.pending.recorded_rolls:
            raise GameError("Concede before the next opposed roll, not after dice are revealed.")
        self.choice(
            b,
            "concession",
            "Choose your negotiated loss.",
            [
                option("custody", "Surrender into custody", "concede", terms="custody"),
                option(
                    "abandon",
                    "Abandon the mission and leave with personal belongings",
                    "concede",
                    terms="abandon",
                ),
            ],
            "awaiting_outcome_choice",
        )

    def offer_concession(self) -> str:
        p = self.repository.pending()
        if p and p.recorded_rolls:
            raise GameError("Concession is unavailable after the roll is revealed.")
        state = self.repository.load()
        if state.conflict is None:
            raise GameError("No conflict is active.")
        p = p or self.fresh("concession", phase="awaiting_outcome_choice")
        self.concession_choice(self.builder(p))
        self.persist(p)
        return self.describe(p)

    def cancel(self) -> str:
        p = self.repository.pending()
        if p is None:
            return "No pending interaction."
        self.repository.cancel(p)
        return "Pending input canceled; world unchanged."
