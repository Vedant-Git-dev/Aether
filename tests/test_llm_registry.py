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
    # _env_file=None keeps a developer's real .env out of the test.
    return Settings(_env_file=None, **kwargs)


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
            ProviderRegistry.from_config(
                _settings(), LLMConfig(provider=provider_name)
            )


def test_unknown_provider_name_raises() -> None:
    with pytest.raises(ProviderError, match="unknown llm.provider"):
        # model_construct bypasses pydantic validation to exercise the
        # registry's own guard against an unvalidated config.
        bad = LLMConfig.model_construct(provider="nope", model="m")
        ProviderRegistry.from_config(_settings(), bad)


def test_salience_role_uses_override_model() -> None:
    registry = ProviderRegistry.from_config(
        _settings(anthropic_api_key="k"),
        LLMConfig(provider="anthropic", model="claude-opus-5", salience_model="claude-haiku-4-5-20251001"),
    )
    assert registry.for_role("reasoning").model == "claude-opus-5"
    assert registry.for_role("salience").model == "claude-haiku-4-5-20251001"


def test_salience_role_defaults_to_reasoning_provider() -> None:
    registry = ProviderRegistry.from_config(
        _settings(anthropic_api_key="k"), LLMConfig()
    )
    assert registry.for_role("salience") is registry.for_role("reasoning")


def test_vision_role_defaults_to_reasoning_provider() -> None:
    # Hosted providers are vision-capable, so vision falls back to default.
    registry = ProviderRegistry.from_config(
        _settings(anthropic_api_key="k"), LLMConfig()
    )
    assert registry.for_role("vision") is registry.for_role("reasoning")


def test_vision_role_none_for_ollama_without_vision_support() -> None:
    registry = ProviderRegistry.from_config(
        _settings(), LLMConfig(provider="ollama", model="llama3.3")
    )
    assert registry.for_role("reasoning") is not None
    assert registry.for_role("vision") is None


def test_ollama_vision_flag_and_explicit_vision_model() -> None:
    # The config flag opts the default model into vision.
    flagged = ProviderRegistry.from_config(
        _settings(), LLMConfig(provider="ollama", model="llava", ollama_vision=True)
    )
    assert flagged.for_role("vision") is flagged.for_role("reasoning")

    # An explicit vision_model is trusted to accept images.
    explicit = ProviderRegistry.from_config(
        _settings(), LLMConfig(provider="ollama", model="llama3.3", vision_model="qwen2.5vl:7b")
    )
    vision = explicit.for_role("vision")
    assert vision is not None
    assert vision is not explicit.for_role("reasoning")
    assert vision.model == "qwen2.5vl:7b"
    assert vision.supports_vision is True


def test_unknown_role_falls_back_to_reasoning() -> None:
    registry = ProviderRegistry.from_config(
        _settings(anthropic_api_key="k"), LLMConfig()
    )
    assert registry.for_role("nonexistent-role") is registry.for_role("reasoning")


def test_registry_providers_satisfy_protocol() -> None:
    registry = ProviderRegistry.from_config(
        _settings(anthropic_api_key="k"), LLMConfig()
    )
    assert isinstance(registry.for_role("reasoning"), Provider)
