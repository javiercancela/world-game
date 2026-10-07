import time
from importlib.resources import files
from typing import Any

from openai import APIConnectionError, APIStatusError, OpenAI, OpenAIError
from pydantic import BaseModel, ValidationError

from world_game.domain.common import canonical, digest
from world_game.domain.proposals import ModelRequest, ModelResult, ModelRole, Usage
from world_game.models.interfaces import ProviderError


def provider_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Translate Pydantic's discriminators to the provider's JSON Schema subset.

    The domain retains discriminated unions and strict validation. Only the
    wire schema uses anyOf and explicitly required nullable/defaulted fields.
    """
    schema = model.model_json_schema()

    def visit(node: object) -> None:
        if isinstance(node, dict):
            node.pop("discriminator", None)
            node.pop("default", None)
            if "oneOf" in node:
                node["anyOf"] = node.pop("oneOf")
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = sorted(node.get("properties", {}))
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(schema)
    return schema


class OpenAIModel:
    def __init__(
        self, api_key: str, model: str, timeout: float = 30, context_tokens: int = 8000
    ) -> None:
        self.client = OpenAI(api_key=api_key, timeout=timeout, max_retries=0)
        self.model = model
        self.context_tokens = context_tokens
        self.timeout = timeout

    def cache_identity(self, role: ModelRole) -> str:
        return digest({"provider": "openai", "model": self.model, "role": role})

    def generate(
        self,
        *,
        role: ModelRole,
        request: ModelRequest,
        response_type: type[BaseModel],
        call_key: str,
        timeout_seconds: float | None = None,
    ) -> ModelResult:
        instructions = files("world_game").joinpath(f"models/prompts/{role}.txt").read_text()
        narrowed = request.model_copy(deep=True)
        # Conservative UTF-8 byte estimate; mandatory data never silently disappears.
        while (
            len((instructions + canonical(narrowed)).encode()) > self.context_tokens * 3
            and narrowed.context.recent_utterances
        ):
            narrowed.context.recent_utterances.pop(0)
        if len((instructions + canonical(narrowed)).encode()) > self.context_tokens * 3:
            raise ProviderError("ContextTooLarge: narrow the action before retrying.")
        start = time.monotonic()
        client = (
            self.client.with_options(timeout=min(self.timeout, timeout_seconds))
            if timeout_seconds is not None
            else self.client
        )
        try:
            response = client.responses.create(
                model=self.model,
                instructions=instructions,
                input=canonical(narrowed),
                text={
                    "format": {
                        "type": "json_schema",
                        "name": response_type.__name__,
                        "strict": True,
                        "schema": provider_schema(response_type),
                    }
                },
                store=False,
                max_output_tokens=1800,
            )
            if response.status != "completed" or not response.output_text:
                raise ProviderError(
                    "Provider refused or returned an incomplete structured response."
                )
            payload = response_type.model_validate_json(response.output_text)
        except (OpenAIError, ValidationError) as error:
            # SDK errors may contain headers: expose only their class, never their body.
            raise ProviderError(
                f"Provider call failed ({type(error).__name__}); use /retry or /quit.",
                retryable=isinstance(error, APIConnectionError)
                or isinstance(error, APIStatusError)
                and (error.status_code == 429 or error.status_code >= 500),
            ) from error
        usage = response.usage
        return ModelResult(
            payload_json=canonical(payload),
            provider="openai",
            model=self.model,
            provider_request_id=response.id,
            latency_ms=int((time.monotonic() - start) * 1000),
            usage=Usage(
                input_tokens=usage.input_tokens if usage else 0,
                output_tokens=usage.output_tokens if usage else 0,
            ),
            context_hash=digest(narrowed),
            prompt_hash=digest(instructions),
            role=role,
            request_json=canonical(narrowed),
        )
