import json

import httpx
import pytest
from openai import OpenAI

from world_game.domain.common import canonical, digest
from world_game.domain.intents import Intent, IntentResult
from world_game.domain.proposals import (
    DescriptionResult,
    ModelRequest,
    NarrationResult,
    NpcProposal,
)
from world_game.engine.actions import PC
from world_game.engine.queries import Viewer, VisibleUtterance, project
from world_game.models.interfaces import ProviderError
from world_game.models.local_provider import LocalModel
from world_game.story.loader import new_state


@pytest.fixture
def local_model():
    clients = []

    def make(response, status=200, **options):
        calls = []

        def handle(request):
            calls.append(request)
            if isinstance(response, Exception):
                raise response
            return httpx.Response(status, json=response)

        adapter = LocalModel("test-model", **options)
        adapter.client.close()
        adapter.client = OpenAI(
            base_url=adapter.base_url,
            api_key="test-local-key",
            timeout=adapter.timeout,
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(handle)),
        )
        clients.append(adapter.client)
        return adapter, calls

    yield make
    for client in clients:
        client.close()


def request(package):
    return ModelRequest(
        context=project(
            new_state(package, "local-test", "00" * 32), Viewer(actor_id=PC), "interpreter"
        ),
        text="wait",
    )


def completion(content, finish="stop", refusal=None, usage=True):
    return {
        "id": "local-request-1",
        "object": "chat.completion",
        "created": 1,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "message": {"role": "assistant", "content": content, "refusal": refusal},
            }
        ],
        "usage": {"prompt_tokens": 42, "completion_tokens": 17, "total_tokens": 59}
        if usage
        else None,
    }


@pytest.mark.parametrize(
    "role,payload",
    [
        ("interpreter", IntentResult(payload=Intent(handler_id="wait"))),
        (
            "npc",
            NpcProposal(
                selected_response="gate.bluff",
                utterance="Go on, then.",
                claim_refs=[],
                disclosure_refs=[],
                offer_template_id=None,
            ),
        ),
        (
            "narrator",
            NarrationResult(prose="You wait.", used_event_ids=[], mentioned_entity_ids=[]),
        ),
        (
            "describer",
            DescriptionResult(prose="The gate is closed.", mentioned_entity_ids=["portal:gate"]),
        ),
    ],
)
def test_local_adapter_uses_chat_schema_and_validated_metadata(package, local_model, role, payload):
    adapter, calls = local_model(completion(canonical(payload)), base_url="http://localhost:9090")
    original = request(package)
    record = adapter.generate(
        role=role, request=original, response_type=type(payload), call_key="test", timeout_seconds=2
    )
    assert type(payload).model_validate_json(record.payload_json) == payload
    assert record.provider == "local" and record.model == "test-model"
    assert record.provider_request_id == "local-request-1"
    assert record.usage.input_tokens == 42 and record.usage.output_tokens == 17
    assert record.context_hash == digest(original) and record.role == role
    assert "test-local-key" not in canonical(record)
    assert len(calls) == 1 and str(calls[0].url) == "http://localhost:9090/v1/chat/completions"
    assert calls[0].extensions["timeout"]["read"] == 2
    wire = json.loads(calls[0].content)
    assert wire["model"] == "test-model" and wire["reasoning_effort"] == "none"
    assert wire["max_tokens"] == 1800 and wire["temperature"] == 0
    assert wire["response_format"]["json_schema"]["strict"] is True
    assert wire["response_format"]["json_schema"]["name"] == type(payload).__name__
    assert wire["messages"][0]["role"] == "system"
    assert (
        canonical(wire["response_format"]["json_schema"]["schema"])
        in wire["messages"][0]["content"]
    )
    assert wire["messages"][1]["content"] == canonical(original)


@pytest.mark.parametrize(
    "response",
    [
        completion(""),
        completion("{}", finish="length"),
        completion("{}", refusal="declined"),
        completion("not JSON"),
        completion('{"payload":{"kind":"action","handler_id":"sql"}}'),
        {**completion("{}"), "choices": []},
    ],
)
def test_invalid_refused_or_truncated_response_is_rejected(package, local_model, response):
    adapter, _ = local_model(response)
    with pytest.raises(ProviderError) as caught:
        adapter.generate(
            role="interpreter",
            request=request(package),
            response_type=IntentResult,
            call_key="test",
        )
    assert not caught.value.retryable


@pytest.mark.parametrize("status,retryable", [(400, False), (401, False), (429, True), (503, True)])
def test_status_errors_are_sanitized_and_sdk_does_not_retry(
    package, local_model, status, retryable
):
    adapter, calls = local_model({"error": {"message": "private-token-content"}}, status=status)
    with pytest.raises(ProviderError) as caught:
        adapter.generate(
            role="interpreter",
            request=request(package),
            response_type=IntentResult,
            call_key="test",
        )
    assert caught.value.retryable is retryable and len(calls) == 1
    assert "private-token-content" not in str(caught.value)


def test_connection_failure_is_retryable(package, local_model):
    adapter, calls = local_model(httpx.ConnectError("private-token-content"))
    with pytest.raises(ProviderError) as caught:
        adapter.generate(
            role="interpreter",
            request=request(package),
            response_type=IntentResult,
            call_key="test",
        )
    assert caught.value.retryable and len(calls) == 1
    assert "private-token-content" not in str(caught.value)


def test_context_budget_includes_schema_and_never_drops_mandatory_data(package, local_model):
    adapter, calls = local_model(None, context_tokens=1)
    with pytest.raises(ProviderError, match="ContextTooLarge"):
        adapter.generate(
            role="interpreter",
            request=request(package),
            response_type=IntentResult,
            call_key="test",
        )
    assert calls == []


def test_context_trimming_does_not_mutate_request(package, local_model):
    adapter, calls = local_model(
        completion(canonical(IntentResult(payload=Intent(handler_id="wait"))))
    )
    original = request(package)
    original.context.recent_utterances = [
        VisibleUtterance(id=f"utterance:{i}", speaker_id=PC, text="x" * 4000) for i in range(10)
    ]
    before = canonical(original)
    adapter.generate(
        role="interpreter", request=original, response_type=IntentResult, call_key="test"
    )
    sent = json.loads(json.loads(calls[0].content)["messages"][1]["content"])
    assert len(sent["context"]["recent_utterances"]) < 10
    assert canonical(original) == before


def test_missing_usage_and_optional_reasoning_parameter(package, local_model):
    adapter, calls = local_model(
        completion(canonical(IntentResult(payload=Intent(handler_id="wait"))), usage=False),
        reasoning_effort=None,
        max_output_tokens=256,
        timeout=1,
    )
    record = adapter.generate(
        role="interpreter",
        request=request(package),
        response_type=IntentResult,
        call_key="test",
        timeout_seconds=10,
    )
    assert record.usage.input_tokens == 0 and record.usage.output_tokens == 0
    wire = json.loads(calls[0].content)
    assert "reasoning_effort" not in wire and wire["max_tokens"] == 256
    assert calls[0].extensions["timeout"]["read"] == 1


def test_cache_identity_separates_servers_models_and_generation_settings(local_model):
    first, _ = local_model(None)
    equivalent, _ = local_model(None, base_url="http://127.0.0.1:8080/")
    assert first.cache_identity("interpreter") == equivalent.cache_identity("interpreter")
    assert first.cache_identity("interpreter") != first.cache_identity("npc")
    for options in [
        {"base_url": "http://localhost:9090"},
        {"context_tokens": 4000},
        {"max_output_tokens": 200},
        {"reasoning_effort": "high"},
    ]:
        different, _ = local_model(None, **options)
        assert first.cache_identity("interpreter") != different.cache_identity("interpreter")
    equivalent.model = "other-model"
    assert first.cache_identity("interpreter") != equivalent.cache_identity("interpreter")
