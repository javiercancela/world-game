"""A dedicated, read-only agent for first-glance scene descriptions."""

from importlib.resources import files

from pydantic import ValidationError

from world_game.domain.common import digest
from world_game.domain.proposals import DescriptionResult, ModelRequest
from world_game.engine.queries import ContextView
from world_game.models.interfaces import ProviderError, StructuredModel


def scene_fallback(view: ContextView) -> str:
    """Render only observable scene details when a model is unavailable."""
    lines = []
    for e in sorted(view.visible_entities, key=lambda e: (e.kind != "location", e.id)):
        if e.kind == "location":
            lines.append(e.description or f"You are at the {e.name}.")
        elif e.open is not None:
            if e.description:
                lines.append(e.description)
            lines.append(f"The {e.name.lower()} is {'open' if e.open else 'closed'}.")
        elif e.description.startswith(("A ", "An ")):
            detail = e.description[0].lower() + e.description[1:].rstrip(".")
            lines.append(f"You see {e.name}, {detail}.")
        else:
            lines.append(f"You see {e.name} nearby.")
            if e.description:
                lines.append(e.description)
    return " ".join(lines) or "You cannot see your surroundings."


class DescriptionAgent:
    def __init__(self, provider: StructuredModel, timeout_seconds: float = 30) -> None:
        self.provider = provider
        self.timeout_seconds = timeout_seconds
        self.cached: tuple[str, str] | None = None

    def describe(self, view: ContextView) -> str:
        if view.purpose != "description":
            raise ValueError("The description agent requires a first-glance scene projection.")
        fallback = scene_fallback(view)
        request = ModelRequest(
            context=view,
            text="Describe only what the player can see at first glance.",
            fallback=fallback,
        )
        key = "description:" + digest(
            {
                "request": request,
                "provider": self.provider.cache_identity("describer"),
                "prompt": files("world_game").joinpath("models/prompts/describer.txt").read_text(),
                "schema": DescriptionResult.model_json_schema(),
            }
        )
        if self.cached and self.cached[0] == key:
            return self.cached[1]
        try:
            record = self.provider.generate(
                role="describer",
                request=request,
                response_type=DescriptionResult,
                call_key=key,
                timeout_seconds=self.timeout_seconds,
            )
            result = DescriptionResult.model_validate_json(record.payload_json)
            if not result.prose.strip() or not set(result.mentioned_entity_ids) <= {
                e.id for e in view.visible_entities
            }:
                return fallback
            self.cached = (key, result.prose)
            return result.prose
        except (ProviderError, ValidationError, KeyboardInterrupt):
            return fallback
