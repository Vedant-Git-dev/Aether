"""Registry resolution tests — client construction only, no network."""

import pytest

from aether.config import LLMConfig, Settings
from aether.llm.anthropic_provider import AnthropicProvider
from aether.llm.base import Provider, ProviderError
from aether.llm.gemini_provider import GeminiProvider
from aether.llm.ollama_provider import OllamaProvider
from aether.llm.openai_provider import OpenAIProvider
from aether.llm.registry import ProviderRegistry


def _settings(**kwargs) -> Settings:
    # a developer's real .env stays out of the test.
    return Settings.without_env_file(**kwargs)


def test_registry_builds_each_configured_provider() -> None:
    cases = [
        ("anthropic", AnthropicProvider, {"anthropic_api_key": "sk-test"}),
        ("openai", OpenAIProvider, {"openai_api_key": "sk-test"}),
        ("gemini", GeminiProvider, {"gemini_api_key": "test"}),
        ("ollama", OllamaProvider, {}),
    ]
    for provider_name, cls, settings_kwargs in cases:
        registry = ProviderRegistry.from_config(
            _settings(**settings_kwargs),
            LLMConfig(provider=provider_name, model="m1"),
        )
        provider = registry.for_role("reasoning")
        assert isinstance(provider, cls)
        assert provider.name == provider_name
        assert provider.model == "m1"


def test_missing_api_key_error_names_the_variable() -> None:
    for provider_name, env_var in [
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("openai", "OPENAI_API_KEY"),
        ("gemini", "GEMINI_API_KEY"),
    ]:
        with pytest.raises(ProviderError, match=env_var):
            ProviderRegistry.from_config(_settings(), LLMConfig(provider=provider_name))


def test_unknown_provider_name_raises() -> None:
    with pytest.raises(ProviderError, match="unknown llm.provider"):
        # model_construct bypasses pydantic validation to exercise the
        # registry's own guard against an unvalidated config.
        bad = LLMConfig.model_construct(provider="nope", model="m")
        ProviderRegistry.from_config(_settings(), bad)


def test_salience_role_uses_override_model() -> None:
    registry = ProviderRegistry.from_config(
        _settings(anthropic_api_key="k"),
        LLMConfig(
            provider="anthropic", model="claude-opus-5", salience_model="claude-haiku-4-5-20251001"
        ),
    )
    assert registry.for_role("reasoning").model == "claude-opus-5"
    assert registry.for_role("salience").model == "claude-haiku-4-5-20251001"


def test_salience_role_defaults_to_reasoning_provider() -> None:
    registry = ProviderRegistry.from_config(_settings(anthropic_api_key="k"), LLMConfig())
    assert registry.for_role("salience") is registry.for_role("reasoning")


def test_perception_roles_are_off_by_default() -> None:
    # The tri-state knobs default to None — every medium is off until the
    # user turns one on, whatever the provider can do.
    registry = ProviderRegistry.from_config(_settings(anthropic_api_key="k"), LLMConfig())
    assert registry.for_role("reasoning") is not None
    assert registry.for_role("image") is None
    assert registry.for_role("audio") is None
    assert registry.for_role("video") is None


def test_base_knob_routes_a_medium_to_the_main_model() -> None:
    registry = ProviderRegistry.from_config(
        _settings(anthropic_api_key="k"),
        LLMConfig(image_model="base", audio_model="base", video_model="base"),
    )
    assert registry.for_role("image") is registry.for_role("reasoning")
    assert registry.for_role("audio") is registry.for_role("reasoning")
    assert registry.for_role("video") is registry.for_role("reasoning")


def test_explicit_knob_builds_its_own_provider() -> None:
    # A specific model is a deliberate choice — its own provider instance,
    # whether or not the base model could ever do the job.
    explicit = ProviderRegistry.from_config(
        _settings(ollama_base_url="http://localhost:11434/v1"),
        LLMConfig(provider="ollama", model="llama3.3", image_model="qwen2.5vl:7b"),
    )
    image = explicit.for_role("image")
    assert image is not None
    assert image is not explicit.for_role("reasoning")
    assert image.model == "qwen2.5vl:7b"
    assert image.supports_vision is True


def test_ollama_vision_flag_still_gates_the_base_provider() -> None:
    # The flag is the user's own claim about their pulled model — with a
    # "base" image knob it decides whether the base provider may see.
    flagged = ProviderRegistry.from_config(
        _settings(),
        LLMConfig(provider="ollama", model="llava", ollama_vision=True, image_model="base"),
    )
    assert flagged.for_role("image") is flagged.for_role("reasoning")
    assert flagged.for_role("image").supports_vision is True
    unflagged = ProviderRegistry.from_config(
        _settings(), LLMConfig(provider="ollama", model="llava", image_model="base")
    )
    assert unflagged.for_role("reasoning").supports_vision is False


def test_unknown_role_falls_back_to_reasoning() -> None:
    registry = ProviderRegistry.from_config(_settings(anthropic_api_key="k"), LLMConfig())
    assert registry.for_role("nonexistent-role") is registry.for_role("reasoning")


def test_registry_providers_satisfy_protocol() -> None:
    registry = ProviderRegistry.from_config(_settings(anthropic_api_key="k"), LLMConfig())
    assert isinstance(registry.for_role("reasoning"), Provider)
