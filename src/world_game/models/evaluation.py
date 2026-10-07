"""Explicit live corpus runner. Results are observations, never fabricated validation."""

import json
import time
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import ValidationError

from world_game.domain.common import GameError, Value, digest
from world_game.domain.intents import Clarification, Intent, IntentResult
from world_game.domain.proposals import ModelRequest
from world_game.domain.state import Placement, WorldState
from world_game.engine.actions import PC, Ruling, plan_action
from world_game.engine.queries import Viewer, project
from world_game.engine.turns import Coordinator
from world_game.models.interfaces import ProviderError, StructuredModel
from world_game.models.scripted import ScriptedModel
from world_game.persistence.sqlite import SQLiteRepository
from world_game.story.loader import load_story, new_state
from world_game.story.schema import StoryPackage


class CaseResult(Value):
    id: str
    category: str
    correct: bool
    invalid_proposal: bool
    disclosure_failure: bool
    clarification: bool
    latency_ms: int
    input_tokens: int
    output_tokens: int
    guard_rejected: bool = False
    accepted_illegal_transition: bool = False
    error: str | None = None


class EvaluationReport(Value):
    scope: str = "Live interpretation and disclosure checks; real engine admission with scripted reactions. NPC and narration quality are not measured."
    model: str
    prompt_version: str
    cases: list[CaseResult]
    correct_interpretations: int
    invalid_proposal_rate: float
    disclosure_failures: int
    unnecessary_clarification_rate: float
    accepted_illegal_transitions: int
    guard_rejections: int
    passed: bool


def probe_admission(
    package: StoryPackage, state: WorldState, result: IntentResult
) -> tuple[bool, bool]:
    """Run the received proposal through the actual coordinator and event journal."""
    view = project(state, Viewer(actor_id=PC), "interpreter")
    payload = result.payload
    illegal = False
    if isinstance(payload, Intent):
        visible = {e.id for e in view.visible_entities} | {a.id for a in view.known_aspects}
        illegal = not set(
            payload.target_ids
            + payload.presented_item_ids
            + ([payload.adjacent_move_id] if payload.adjacent_move_id else [])
        ) <= visible or not set(payload.claim_refs) <= {f.id for f in view.perceived_facts}
        try:
            plan_action(state, payload, Ruling(rules=package.rules))
        except GameError:
            illegal = True
    with TemporaryDirectory(prefix="world-game-eval-") as directory:
        repo = SQLiteRepository.create(Path(directory) / "case.db", package, state)
        try:
            c = Coordinator(repo, ScriptedModel({"interpreter": result}))
            try:
                c.submit("evaluation proposal")
                for _ in range(10):
                    pending = repo.pending()
                    if (
                        not pending
                        or not pending.current_choice
                        or pending.current_choice.kind != "invoke"
                    ):
                        break
                    c.choose("pass")
                changed = repo.load().world_version > state.world_version
                broken_replay = digest(repo.replay()) != digest(repo.load())
            except GameError:
                changed = repo.load().world_version > state.world_version
                broken_replay = False
            return illegal and not changed, (illegal and changed) or broken_replay
        finally:
            repo.close()


def evaluate(provider: StructuredModel) -> EvaluationReport:
    package = load_story()
    raw = json.loads(
        files("world_game")
        .joinpath("data/stories/gate_at_dusk/fixtures/eval-cases.json")
        .read_text()
    )
    results = []
    model = "unknown"
    prompt = "roles-v1"
    unnecessary = 0
    for case in raw:
        state = new_state(package, "evaluation", "00" * 32)
        if case["scene"] == "courtyard":
            state.entities[PC].placement = Placement(container_id="location:courtyard")
        view = project(state, Viewer(actor_id=PC), "interpreter")
        request = ModelRequest(context=view, text=case["text"])
        started = time.monotonic()
        try:
            record = provider.generate(
                role="interpreter",
                request=request,
                response_type=IntentResult,
                call_key="eval:" + case["id"],
            )
            model, prompt = record.model, record.prompt_version
            payload = IntentResult.model_validate_json(record.payload_json).payload
            clarification = isinstance(payload, Clarification)
            invalid = False
            correct = clarification and case["clarification_allowed"]
            if not isinstance(payload, Clarification):
                known = {e.id for e in view.visible_entities} | {a.id for a in view.known_aspects}
                invalid = not set(
                    payload.target_ids + payload.presented_item_ids
                ) <= known or not set(payload.claim_refs) <= {f.id for f in view.perceived_facts}
                correct = (
                    payload.handler_id in case["handlers"]
                    and (not case["goals"] or payload.goal in case["goals"])
                    and not invalid
                    and (not case.get("require_new_claim") or bool(payload.new_claim))
                    and (not case.get("methods") or payload.method in case["methods"])
                    and (
                        not case.get("expected_targets")
                        or payload.target_ids == case["expected_targets"]
                    )
                )
            leaked = any(
                word.casefold() in record.payload_json.casefold()
                for word in case["forbidden_disclosures"]
            )
            unnecessary += int(clarification and not case["clarification_allowed"])
            rejected, accepted_illegal = probe_admission(
                package, state, IntentResult(payload=payload)
            )
            results.append(
                CaseResult(
                    id=case["id"],
                    category=case["category"],
                    correct=correct and not leaked,
                    invalid_proposal=invalid,
                    disclosure_failure=leaked,
                    clarification=clarification,
                    latency_ms=record.latency_ms,
                    input_tokens=record.usage.input_tokens,
                    output_tokens=record.usage.output_tokens,
                    guard_rejected=rejected,
                    accepted_illegal_transition=accepted_illegal,
                )
            )
        except (ProviderError, ValidationError) as error:
            results.append(
                CaseResult(
                    id=case["id"],
                    category=case["category"],
                    correct=False,
                    invalid_proposal=True,
                    disclosure_failure=False,
                    clarification=False,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    input_tokens=0,
                    output_tokens=0,
                    error=str(error),
                )
            )
    correct_count = sum(c.correct for c in results)
    failures = sum(c.disclosure_failure for c in results)
    illegal_count = sum(c.accepted_illegal_transition for c in results)
    return EvaluationReport(
        model=model,
        prompt_version=prompt,
        cases=results,
        correct_interpretations=correct_count,
        invalid_proposal_rate=sum(c.invalid_proposal for c in results) / len(results),
        disclosure_failures=failures,
        unnecessary_clarification_rate=unnecessary / len(results),
        accepted_illegal_transitions=illegal_count,
        guard_rejections=sum(c.guard_rejected for c in results),
        passed=correct_count >= 27 and failures == 0 and illegal_count == 0,
    )
