import os
from pathlib import Path
from typing import cast

from pydantic import Field, field_validator

from world_game.domain.common import GameError, Value
from world_game.models.interfaces import ProviderName, StructuredModel
from world_game.models.local_provider import LocalModel, ReasoningEffort, api_base_url
from world_game.models.openai_provider import OpenAIModel
from world_game.models.scripted import ScriptedModel

BONSAI_MODEL = "/home/xavi/bonsai/models/bonsai2-gguf/27B/Ternary-Bonsai-2-27B-PQ2_0.gguf"


class Settings(Value):
    provider: ProviderName = "local"
    model: str = Field(default=BONSAI_MODEL, min_length=1)
    base_url: str = "http://127.0.0.1:8080/v1"
    max_output_tokens: int = Field(default=1800, ge=1, le=32000)
    reasoning_effort: ReasoningEffort | None = "none"
    timeout: float = Field(default=30.0, gt=0, le=120)
    call_budget: int = Field(default=8, ge=1, le=20)
    provider_deadline: float = Field(default=120.0, gt=0, le=600)
    context_tokens: int = Field(default=8000, ge=1000, le=32000)
    save_directory: Path = Field(
        default_factory=lambda: Path.home() / ".local/share/world-game/campaigns"
    )

    @field_validator("base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        return api_base_url(value)

    @field_validator("model")
    @classmethod
    def normalize_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("WORLD_GAME_MODEL must be a nonempty server model ID or alias.")
        return value.strip()

    @classmethod
    def environment(cls, provider: str | None = None) -> "Settings":
        provider = provider or os.environ.get("WORLD_GAME_PROVIDER", "local")
        if provider == "openai" and not os.environ.get("WORLD_GAME_MODEL"):
            raise GameError(
                "WORLD_GAME_MODEL is required for live play; choose a Structured Outputs model."
            )
        return cls(
            provider=cast(ProviderName, provider),
            model=os.environ.get("WORLD_GAME_MODEL", BONSAI_MODEL),
            base_url=os.environ.get("WORLD_GAME_BASE_URL", "http://127.0.0.1:8080/v1"),
            max_output_tokens=int(os.environ.get("WORLD_GAME_MAX_OUTPUT_TOKENS", "1800")),
            reasoning_effort=cast(
                ReasoningEffort | None,
                os.environ.get("WORLD_GAME_REASONING_EFFORT", "none") or None,
            ),
            timeout=float(os.environ.get("WORLD_GAME_TIMEOUT", "30")),
            call_budget=int(os.environ.get("WORLD_GAME_CALL_BUDGET", "8")),
            provider_deadline=float(os.environ.get("WORLD_GAME_PROVIDER_DEADLINE", "120")),
            context_tokens=int(os.environ.get("WORLD_GAME_CONTEXT_TOKENS", "8000")),
            save_directory=Path(
                os.environ.get(
                    "WORLD_GAME_SAVE_DIR", str(Path.home() / ".local/share/world-game/campaigns")
                )
            ),
        )

    def build_provider(self) -> StructuredModel:
        if self.provider == "scripted":
            return ScriptedModel()
        if self.provider == "local":
            return LocalModel(
                model=self.model,
                base_url=self.base_url,
                api_key=os.environ.get("WORLD_GAME_API_KEY"),
                timeout=self.timeout,
                context_tokens=self.context_tokens,
                max_output_tokens=self.max_output_tokens,
                reasoning_effort=self.reasoning_effort,
            )
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise GameError(
                "OPENAI_API_KEY is required for live play. Export it or use --provider scripted."
            )
        return OpenAIModel(key, self.model, self.timeout, self.context_tokens)

    def campaign_path(self, name: str) -> Path:
        import re

        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
            raise GameError("Campaign names use 1–64 letters, digits, underscores, or hyphens.")
        return self.save_directory / f"{name}.sqlite3"
