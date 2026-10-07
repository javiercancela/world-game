"""Deterministic offline interpreter and contract fixtures; no world mutation."""

import re

from pydantic import BaseModel

from world_game.domain.common import canonical, digest
from world_game.domain.intents import Clarification, Handler, Intent, IntentResult
from world_game.domain.proposals import (
    DescriptionResult,
    ModelRequest,
    ModelResult,
    ModelRole,
    NarrationResult,
    NpcProposal,
)
from world_game.models.interfaces import ProviderError


def interpret(text: str, request: ModelRequest) -> IntentResult:
    raw = text.strip()
    low = raw.casefold()
    view = request.context
    ids = {e.id for e in view.visible_entities}
    people = [e for e in view.visible_entities if e.kind == "actor" and e.id != view.viewer_id]
    found = [e.id for e in people if re.search(r"\b" + re.escape(e.name.casefold()) + r"\b", low)]
    person = found[0] if len(found) == 1 else people[0].id if len(people) == 1 else None

    def action(
        handler: Handler,
        targets: list[str] | None = None,
        goal: str = "",
        method: str = "",
        items: list[str] | None = None,
    ) -> IntentResult:
        return IntentResult(
            payload=Intent(
                handler_id=handler,
                target_ids=targets or [],
                goal=goal,
                method=method,
                speech=raw,
                presented_item_ids=items or [],
            )
        )

    if (
        "ignore" in low
        and ("rules" in low or "instructions" in low)
        or "sql" in low
        or "json patch" in low
    ):
        return (
            action("speak", [person] if person else [], "question")
            if person
            else IntentResult(
                payload=Clarification(prompt="Choose a supported action in the scene.")
            )
        )
    if low.startswith(("don't ", "do not ", "i don't ", "i do not ")):
        return (
            action("speak", [person], "question")
            if person
            else IntentResult(payload=Clarification(prompt="What would you like to do instead?"))
        )
    # Resolve one consequential action, retaining the full sentence as speech evidence.
    low = re.split(r"\bthen\b|;", low)[0].strip()
    if any(word in low for word in ("wait", "pause", "pass time")):
        return action("wait")
    if any(word in low for word in ("attack", "punch", "strike", "hit oren", "hit sen")):
        return (
            action("attack", [person], "fight")
            if person
            else IntentResult(
                payload=Clarification(
                    prompt="Who do you attack?", candidates=[p.id for p in people]
                )
            )
        )
    if (
        "pick" in low
        and ("lock" in low or "door" in low)
        and not any(word in low for word in ("retrieve", "pull out", "recover my lockpick"))
    ):
        return action("pick_lock", ["portal:records_door"], "lock")
    if any(word in low for word in ("retrieve", "pull out", "recover my lockpick")):
        return action("retrieve_lodged_item", ["item:lockpicks"])
    if "key" in low and any(w in low for w in ("unlock", "use", "open")):
        return action("use_key", ["portal:records_door"])
    if any(w in low for w in ("give", "return the key", "return sen", "deliver", "hand over")):
        item = (
            "item:records_key"
            if "key" in low
            else "item:archive_receipt"
            if "receipt" in low
            else "item:royal_seal"
            if "seal" in low
            else "item:ledger"
            if "ledger" in low
            else None
        )
        if item and person:
            return action("transfer_item", [item, person])
        return IntentResult(
            payload=Clarification(
                prompt="Which item and recipient do you mean?", candidates=[p.id for p in people]
            )
        )
    if any(
        w in low for w in ("take the ledger", "pick up", "collect the ledger", "get the ledger")
    ):
        return action("take_item", ["item:ledger"])
    if any(w in low for w in ("read oren", "read his", "concern", "worry", "worried")):
        if any(w in low for w in ("address", "reassure", "blame won't", "avoid blame")):
            return action(
                "social_overcome", [person or "actor:oren"], "address_concern", "address_concern"
            )
        return action("discover_advantage", [person or "actor:oren"], "concern", "read_concern")
    if any(
        w in low
        for w in (
            "expected",
            "expects me",
            "invited",
            "courier",
            "royal business",
            "show the seal",
            "present my seal",
        )
    ):
        return action(
            "social_overcome",
            [person or "actor:oren"],
            "gain_entry",
            "claim_expected_courier",
            ["item:royal_seal"] if "seal" in low else [],
        )
    if any(
        w in low
        for w in ("let me out", "permit exit", "allow me out", "persuade oren to let me leave")
    ):
        return action("social_overcome", ["actor:oren"], "exit", "honest_exit")
    if "need sen" in low or "see sen" in low:
        return action("speak", ["actor:oren"], "need_sen")
    if any(w in low for w in ("go ", "enter ", "move ", "walk ", "return to", "leave through")):
        for suffix, aliases in [
            ("gatehouse", ("gatehouse", "outside")),
            ("courtyard", ("courtyard",)),
            ("records", ("records", "office")),
        ]:
            if any(a in low for a in aliases):
                return action("move", ["location:" + suffix])
        return IntentResult(
            payload=Clarification(
                prompt="Choose an adjacent destination.",
                candidates=[e.id for e in view.visible_entities if e.kind == "location"],
            )
        )
    if any(w in low for w in ("advantage", "distract", "off balance", "focus", "cover", "wedge")):
        method = (
            "jammed_portal"
            if "wedge" in low
            else "focused"
            if "focus" in low
            else "cover"
            if "cover" in low
            else "off_balance"
        )
        return action(
            "create_advantage",
            [person] if person and method == "off_balance" else [],
            "advantage",
            method,
        )
    if person:
        goal = (
            "help"
            if "help" in low or "borrow" in low or "ask" in low and "key" in low
            else "ledger"
            if "ledger" in low
            else "greet"
            if any(w in low for w in ("hello", "greet", "hi "))
            else "question"
        )
        return action("speak", [person], goal)
    return IntentResult(
        payload=Clarification(
            prompt="Please name a visible person, destination, or supported action.",
            candidates=sorted(ids),
        )
    )


class ScriptedModel:
    def cache_identity(self, role: ModelRole) -> str:
        return digest({"provider": "scripted", "model": "scripted-v1", "role": role})

    def __init__(self, responses: dict[str, str | BaseModel | Exception] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, ModelRequest]] = []

    def generate(
        self,
        *,
        role: ModelRole,
        request: ModelRequest,
        response_type: type[BaseModel],
        call_key: str,
        timeout_seconds: float | None = None,
    ) -> ModelResult:
        self.calls.append((role, request))
        fixture = self.responses.get(call_key, self.responses.get(role))
        if isinstance(fixture, Exception):
            if isinstance(fixture, ProviderError):
                raise fixture
            raise ProviderError(str(fixture))
        if fixture is not None:
            payload = response_type.model_validate_json(
                fixture if isinstance(fixture, str) else canonical(fixture)
            )
        elif role == "interpreter":
            payload = interpret(request.text, request)
        elif role == "npc":
            payload = NpcProposal(
                selected_response=request.permitted_responses[0],
                utterance=request.fallback,
                claim_refs=[],
                disclosure_refs=request.evidence_refs,
                offer_template_id=None,
            )
        elif role == "narrator":
            payload = NarrationResult(
                prose=request.fallback,
                used_event_ids=request.evidence_refs,
                mentioned_entity_ids=[],
            )
        elif role == "describer":
            payload = DescriptionResult(prose=request.fallback, mentioned_entity_ids=[])
        else:
            raise ProviderError("No scripted fixture for this model role.")
        return ModelResult(
            payload_json=canonical(payload),
            provider="scripted",
            model="scripted-v1",
            context_hash=digest(request),
        )
