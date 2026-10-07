import os

import pytest
from pydantic import ValidationError

from world_game.config import BONSAI_MODEL, Settings
from world_game.domain.common import GameError, canonical
from world_game.models.local_provider import LocalModel
from world_game.models.openai_provider import OpenAIModel
from world_game.models.scripted import ScriptedModel


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in list(os.environ):
        if name.startswith("WORLD_GAME_") or name in ("OPENAI_API_KEY", "OPENAI_BASE_URL"):
            monkeypatch.delenv(name)


def test_default_provider_uses_reference_bonsai_server_without_credentials():
    settings = Settings.environment()
    assert settings.provider == "local" and settings.model == BONSAI_MODEL
    adapter = settings.build_provider()
    assert isinstance(adapter, LocalModel)
    assert adapter.base_url == "http://127.0.0.1:8080/v1"
    assert adapter.client.api_key == "local-no-key"
    adapter.client.close()


def test_local_configuration_and_credentials_are_independent_of_openai(monkeypatch):
    monkeypatch.setenv("WORLD_GAME_MODEL", "custom-alias")
    monkeypatch.setenv("WORLD_GAME_BASE_URL", "http://localhost:9090/proxy/")
    monkeypatch.setenv("WORLD_GAME_API_KEY", "local-secret")
    monkeypatch.setenv("WORLD_GAME_MAX_OUTPUT_TOKENS", "512")
    monkeypatch.setenv("WORLD_GAME_REASONING_EFFORT", "high")
    monkeypatch.setenv("OPENAI_API_KEY", "cloud-secret")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    settings = Settings.environment()
    adapter = settings.build_provider()
    assert isinstance(adapter, LocalModel)
    assert adapter.model == "custom-alias" and adapter.base_url == "http://localhost:9090/proxy/v1"
    assert adapter.client.api_key == "local-secret"
    assert adapter.max_output_tokens == 512 and adapter.reasoning_effort == "high"
    assert "local-secret" not in canonical(settings) and "cloud-secret" not in canonical(settings)
    adapter.client.close()


def test_openai_key_is_not_sent_to_local_server(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "cloud-secret")
    adapter = Settings.environment().build_provider()
    assert isinstance(adapter, LocalModel) and adapter.client.api_key == "local-no-key"
    adapter.client.close()


def test_scripted_override_and_empty_reasoning_setting(monkeypatch):
    monkeypatch.setenv("WORLD_GAME_PROVIDER", "openai")
    monkeypatch.setenv("WORLD_GAME_REASONING_EFFORT", "")
    settings = Settings.environment("scripted")
    assert (
        isinstance(settings.build_provider(), ScriptedModel) and settings.reasoning_effort is None
    )


def test_openai_still_requires_explicit_model_and_key(monkeypatch):
    with pytest.raises(GameError, match="WORLD_GAME_MODEL"):
        Settings.environment("openai")
    monkeypatch.setenv("WORLD_GAME_MODEL", "cloud-model")
    with pytest.raises(GameError, match="OPENAI_API_KEY"):
        Settings.environment("openai").build_provider()
    monkeypatch.setenv("OPENAI_API_KEY", "cloud-key")
    adapter = Settings.environment("openai").build_provider()
    assert isinstance(adapter, OpenAIModel) and adapter.model == "cloud-model"
    adapter.client.close()


@pytest.mark.parametrize(
    "name,value",
    [
        ("WORLD_GAME_PROVIDER", "unknown"),
        ("WORLD_GAME_MODEL", " "),
        ("WORLD_GAME_BASE_URL", "file:///tmp/server"),
        ("WORLD_GAME_BASE_URL", "http://"),
        ("WORLD_GAME_BASE_URL", "http://localhost:bad"),
        ("WORLD_GAME_BASE_URL", "http://user:secret@localhost:8080"),
        ("WORLD_GAME_BASE_URL", "http://localhost:8080?key=secret"),
        ("WORLD_GAME_MAX_OUTPUT_TOKENS", "0"),
        ("WORLD_GAME_REASONING_EFFORT", "unknown"),
    ],
)
def test_invalid_local_settings_fail_before_network(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises((ValidationError, ValueError)):
        Settings.environment()
