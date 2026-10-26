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
    with pytest.raises(ProviderError, match="unknown provider"):
        # model_construct bypasses pydantic validation to exercise the
        # registry's own guard against an unvalidated config — a bare id
        # falls to the global provider, whose name is then the bad word
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


def test_salience_role_off_means_no_first_pass() -> None:
    # "off" is the honest stop: no model skims at all — not the main
    # model either — while the main model keeps thinking
    registry = ProviderRegistry.from_config(
        _settings(anthropic_api_key="k"),
        LLMConfig(provider="anthropic", model="claude-opus-5", salience_model="off"),
    )
    assert registry.for_role("salience") is None
    assert registry.for_role("reasoning") is not None


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


def test_ollama_vision_never_gates_a_hosted_provider() -> None:
    # the flag speaks only about the model the user pulled for Ollama —
    # under Gemini (or any hosted provider) it is inert, and a panel copy
    # of the raw config must never read as "no vision model is set"
    registry = ProviderRegistry.from_config(
        _settings(gemini_api_key="test"),
        LLMConfig(
            provider="gemini",
            model="gemini-3.1-flash-lite",
            ollama_vision=False,  # unchecked in the panel — meaningless here
            image_model="base",
            audio_model="base",
            video_model="base",
        ),
    )
    for role in ("image", "audio", "video"):
        assert registry.for_role(role) is registry.for_role("reasoning")
    assert registry.for_role("image").supports_vision is True


def test_unknown_role_falls_back_to_reasoning() -> None:
    registry = ProviderRegistry.from_config(_settings(anthropic_api_key="k"), LLMConfig())
    assert registry.for_role("nonexistent-role") is registry.for_role("reasoning")


# -- provider-prefixed model values: the dropdown's stored form --------------
# "provider/model", split on the first slash — the rest is the id that
# provider's API answers to, so an id may itself contain slashes.


def test_a_prefixed_main_model_routes_to_its_own_provider() -> None:
    # the global provider says anthropic, but the picked model says gemini
    # — the model's own prefix wins
    registry = ProviderRegistry.from_config(
        _settings(anthropic_api_key="k", gemini_api_key="g"),
        LLMConfig(provider="anthropic", model="gemini/gemini-2.5-flash"),
    )
    reasoning = registry.for_role("reasoning")
    assert isinstance(reasoning, GeminiProvider)
    assert reasoning.model == "gemini-2.5-flash"


def test_the_first_slash_is_the_split_openai_submodel() -> None:
    registry = ProviderRegistry.from_config(
        _settings(openai_api_key="sk-test"),
        LLMConfig(provider="openai", model="openai/gpt/gpt-5.6"),
    )
    reasoning = registry.for_role("reasoning")
    assert isinstance(reasoning, OpenAIProvider)
    assert reasoning.model == "gpt/gpt-5.6"  # the id keeps its own slash


def test_a_prefixed_perception_knob_builds_that_provider() -> None:
    registry = ProviderRegistry.from_config(
        _settings(anthropic_api_key="k", gemini_api_key="g"),
        LLMConfig(
            provider="anthropic", model="claude-opus-5", image_model="gemini/gemini-2.5-flash"
        ),
    )
    image = registry.for_role("image")
    assert isinstance(image, GeminiProvider)
    assert image.model == "gemini-2.5-flash"


def test_a_prefixed_knob_ignores_the_global_provider() -> None:
    # the global provider has no say in a value that carries its own —
    # even when the two disagree, the prefix is the routing truth
    registry = ProviderRegistry.from_config(
        _settings(openai_api_key="sk-test", ollama_base_url="http://localhost:11434/v1"),
        LLMConfig(provider="openai", model="gpt-5.6", image_model="ollama/qwen2.5vl:7b"),
    )
    image = registry.for_role("image")
    assert isinstance(image, OllamaProvider)
    assert image.model == "qwen2.5vl:7b"
    assert image.supports_vision is True  # a deliberate choice sees


def test_unknown_prefixed_provider_raises_plainly() -> None:
    with pytest.raises(ProviderError, match="unknown provider in model name"):
        ProviderRegistry.from_config(
            _settings(anthropic_api_key="k"),
            LLMConfig(provider="anthropic", model="nope/m1"),
        )


def test_an_odd_prefixed_value_raises_plainly() -> None:
    with pytest.raises(ProviderError, match="odd model name"):
        ProviderRegistry.from_config(
            _settings(anthropic_api_key="k"),
            LLMConfig(provider="anthropic", model="gemini/"),
        )


# -- the sentinel words: off and default, beside the legacy base ------------


def test_off_knob_leaves_the_medium_unseen() -> None:
    registry = ProviderRegistry.from_config(
        _settings(anthropic_api_key="k"),
        LLMConfig(image_model="off", audio_model="off", video_model="off"),
    )
    assert registry.for_role("image") is None
    assert registry.for_role("audio") is None
    assert registry.for_role("video") is None


def test_default_knob_routes_a_medium_to_the_main_model() -> None:
    registry = ProviderRegistry.from_config(
        _settings(anthropic_api_key="k"),
        LLMConfig(image_model="default", audio_model="default", video_model="default"),
    )
    assert registry.for_role("image") is registry.for_role("reasoning")
    assert registry.for_role("audio") is registry.for_role("reasoning")
    assert registry.for_role("video") is registry.for_role("reasoning")


def test_default_salience_is_the_main_model_not_its_own_provider() -> None:
    # "default" resolves to the main model when providers are built, never
    # at save time — and never as a model literally named "default"
    registry = ProviderRegistry.from_config(
        _settings(anthropic_api_key="k"),
        LLMConfig(model="claude-opus-5", salience_model="default"),
    )
    assert registry.for_role("salience") is registry.for_role("reasoning")
    assert registry.for_role("salience").model == "claude-opus-5"


def test_bare_ids_still_route_through_the_global_provider() -> None:
    # the legacy spelling: an id with no prefix belongs to llm.provider
    registry = ProviderRegistry.from_config(
        _settings(anthropic_api_key="k"),
        LLMConfig(provider="anthropic", model="claude-opus-5", image_model="claude-haiku-4-5"),
    )
    assert isinstance(registry.for_role("reasoning"), AnthropicProvider)
    assert registry.for_role("image").model == "claude-haiku-4-5"


def test_ollama_vision_follows_the_main_models_own_provider() -> None:
    # the flag speaks about the model the user pulled for Ollama — it
    # gates vision only when the main model actually routes there,
    # prefix or global
    settings = _settings(anthropic_api_key="k", ollama_base_url="http://localhost:11434/v1")
    flagged = ProviderRegistry.from_config(
        settings,
        LLMConfig(provider="anthropic", model="ollama/llava", ollama_vision=True),
    )
    assert flagged.for_role("reasoning").supports_vision is True
    unflagged = ProviderRegistry.from_config(
        settings,
        LLMConfig(provider="anthropic", model="ollama/llava", ollama_vision=False),
    )
    assert unflagged.for_role("reasoning").supports_vision is False
    # and it stays inert when the main model is hosted after all
    hosted = ProviderRegistry.from_config(
        settings,
        LLMConfig(provider="anthropic", model="claude-opus-5", ollama_vision=False),
    )
    assert hosted.for_role("reasoning").supports_vision is True


def test_registry_providers_satisfy_protocol() -> None:
    registry = ProviderRegistry.from_config(_settings(anthropic_api_key="k"), LLMConfig())
    assert isinstance(registry.for_role("reasoning"), Provider)
