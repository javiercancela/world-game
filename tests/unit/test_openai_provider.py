from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError

from world_game.domain.common import canonical
from world_game.domain.intents import Intent, IntentResult
from world_game.domain.proposals import (
    DescriptionResult,
    ModelRequest,
    NarrationResult,
    NpcProposal,
)
from world_game.engine.actions import PC
from world_game.engine.queries import Viewer, project
from world_game.models.interfaces import ProviderError
from world_game.models.openai_provider import OpenAIModel, provider_schema
from world_game.story.loader import new_state


def request(package):
    return ModelRequest(
        context=project(
            new_state(package, "provider-test", "00" * 32), Viewer(actor_id=PC), "interpreter"
        ),
        text="wait",
    )


class FakeResponses:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def model(response, budget=8000):
    adapter = OpenAIModel("unused-test-key", "test-model", context_tokens=budget)
    fake = FakeResponses(response)
    adapter.client = SimpleNamespace(responses=fake)
    return adapter, fake


@pytest.mark.parametrize(
    "contract", [IntentResult, NpcProposal, NarrationResult, DescriptionResult]
)
def test_provider_schema_strict_objects_and_supported_unions(contract):
    def check(node):
        if isinstance(node, dict):
            assert "oneOf" not in node and "discriminator" not in node and "default" not in node
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for child in node.values():
                check(child)
        elif isinstance(node, list):
            for child in node:
                check(child)

    schema = provider_schema(contract)
    assert schema["type"] == "object"
    check(schema)


def test_adapter_sends_schema_and_returns_validated_metadata(package):
    payload = IntentResult(payload=Intent(handler_id="wait"))
    adapter, fake = model(
        SimpleNamespace(
            status="completed",
            output_text=canonical(payload),
            id="request-1",
            usage=SimpleNamespace(input_tokens=42, output_tokens=17),
        )
    )
    result = adapter.generate(
        role="interpreter", request=request(package), response_type=IntentResult, call_key="call-1"
    )
    assert IntentResult.model_validate_json(result.payload_json) == payload
    assert result.provider_request_id == "request-1" and result.usage.input_tokens == 42
    wire = fake.calls[0]
    assert wire["store"] is False and wire["model"] == "test-model"
    assert wire["text"]["format"]["strict"] is True
    assert "unused-test-key" not in canonical(result)


@pytest.mark.parametrize(
    "status,text",
    [
        ("completed", ""),
        ("incomplete", "{}"),
        ("completed", '{"payload":{"kind":"action","handler_id":"sql"}}'),
    ],
)
def test_refusal_incomplete_or_malformed_output_fails_safely(package, status, text):
    adapter, _ = model(SimpleNamespace(status=status, output_text=text))
    with pytest.raises(ProviderError):
        adapter.generate(
            role="interpreter",
            request=request(package),
            response_type=IntentResult,
            call_key="call-1",
        )


def test_connection_error_is_retryable_without_error_body(package):
    error = APIConnectionError(
        message="private-token-content", request=httpx.Request("POST", "https://api.openai.com")
    )
    adapter, _ = model(error)
    with pytest.raises(ProviderError) as caught:
        adapter.generate(
            role="interpreter",
            request=request(package),
            response_type=IntentResult,
            call_key="call-1",
        )
    assert caught.value.retryable and "private-token-content" not in str(caught.value)


def test_mandatory_context_budget_fails_before_call(package):
    adapter, fake = model(None, budget=1)
    with pytest.raises(ProviderError, match="ContextTooLarge"):
        adapter.generate(
            role="interpreter",
            request=request(package),
            response_type=IntentResult,
            call_key="call-1",
        )
    assert fake.calls == []
