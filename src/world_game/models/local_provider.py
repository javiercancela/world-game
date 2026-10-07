"""Structured proposals through a local OpenAI-compatible Chat Completions server."""

import time
from importlib.resources import files
from typing import Literal
from urllib.parse import urlsplit

from openai import APIConnectionError, APIStatusError, OpenAI, OpenAIError, omit
from pydantic import BaseModel, ValidationError

from world_game.domain.common import canonical, digest
from world_game.domain.proposals import ModelRequest, ModelResult, ModelRole, Usage
from world_game.models.interfaces import ProviderError
from world_game.models.openai_provider import provider_schema

ReasoningEffort = Literal["none", "low", "medium", "high"]


def api_base_url(value: str) -> str:
    """Accept a server root or an API URL, including a reverse-proxy prefix."""
    value = value.strip().rstrip("/")
    url = urlsplit(value)
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
    ):
        raise ValueError(
            "WORLD_GAME_BASE_URL must be an HTTP(S) server URL without credentials, query, or fragment."
        )
    # Accessing port also validates malformed/non-numeric port numbers.
    _ = url.port
    return value if url.path.endswith("/v1") else value + "/v1"


class LocalModel:
    def __init__(
        self,
        model: str,
        base_url: str = "http://127.0.0.1:8080/v1",
        api_key: str | None = None,
        timeout: float = 30,
        context_tokens: int = 8000,
        max_output_tokens: int = 1800,
        reasoning_effort: ReasoningEffort | None = "none",
    ) -> None:
        self.base_url = api_base_url(base_url)
        self.client = OpenAI(
            base_url=self.base_url,
            api_key=api_key or "local-no-key",
            timeout=timeout,
            max_retries=0,
        )
        self.model = model
        self.timeout = timeout
        self.context_tokens = context_tokens
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort

    def cache_identity(self, role: ModelRole) -> str:
        return digest(
            {
                "provider": "local",
                "base_url": self.base_url,
                "model": self.model,
                "role": role,
                "context_tokens": self.context_tokens,
                "max_output_tokens": self.max_output_tokens,
                "reasoning_effort": self.reasoning_effort,
            }
        )

    def generate(
        self,
        *,
        role: ModelRole,
        request: ModelRequest,
        response_type: type[BaseModel],
        call_key: str,
        timeout_seconds: float | None = None,
    ) -> ModelResult:
        schema = provider_schema(response_type)
        instructions = files("world_game").joinpath(f"models/prompts/{role}.txt").read_text()
        # llama.cpp constrains generation but does not inject the schema into the prompt.
        instructions += "\nReturn only JSON matching this schema:\n" + canonical(schema)
        narrowed = request.model_copy(deep=True)
        while (
            len((instructions + canonical(narrowed)).encode()) > self.context_tokens * 3
            and narrowed.context.recent_utterances
        ):
            narrowed.context.recent_utterances.pop(0)
        if len((instructions + canonical(narrowed)).encode()) > self.context_tokens * 3:
            raise ProviderError("ContextTooLarge: narrow the action before retrying.")
        started = time.monotonic()
        client = (
            self.client.with_options(timeout=min(self.timeout, timeout_seconds))
            if timeout_seconds is not None
            else self.client
        )
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": canonical(narrowed)},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": response_type.__name__,
                        "strict": True,
                        "schema": schema,
                    },
                },
                max_tokens=self.max_output_tokens,
                temperature=0,
                reasoning_effort=self.reasoning_effort if self.reasoning_effort else omit,
            )
            if not response.choices:
                raise ProviderError("Local provider returned no structured response.")
            choice = response.choices[0]
            if (
                choice.finish_reason != "stop"
                or choice.message.refusal
                or not choice.message.content
            ):
                raise ProviderError(
                    "Local provider refused or returned an incomplete structured response."
                )
            payload = response_type.model_validate_json(choice.message.content)
        except (OpenAIError, ValidationError) as error:
            # Error bodies/headers can include credentials; expose only the exception class.
            raise ProviderError(
                f"Local provider call failed ({type(error).__name__}); check WORLD_GAME_BASE_URL and the server, then use /retry or /quit.",
                retryable=isinstance(error, APIConnectionError)
                or isinstance(error, APIStatusError)
                and (error.status_code == 429 or error.status_code >= 500),
            ) from error
        usage = response.usage
        return ModelResult(
            payload_json=canonical(payload),
            provider="local",
            model=response.model or self.model,
            provider_request_id=response.id,
            latency_ms=int((time.monotonic() - started) * 1000),
            usage=Usage(
                input_tokens=usage.prompt_tokens if usage else 0,
                output_tokens=usage.completion_tokens if usage else 0,
            ),
            context_hash=digest(narrowed),
            prompt_hash=digest(instructions),
            role=role,
            request_json=canonical(narrowed),
        )
