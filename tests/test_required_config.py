"""The required-configuration rules, shared by boot and the setup wizard."""

from __future__ import annotations

from rich.console import Console

from aether.config import AppConfig, Settings
from aether.config_validation import (
    is_placeholder,
    normalize_database_url,
    startup_problems,
)
from aether.setup.capabilities import Glyphs
from aether.setup.plan import SetupPlan
from aether.setup.prompts import UI
from aether.setup.stages import SetupContext
from aether.setup.wizard import required_errors

UNICODE = Glyphs(selected="●", unselected="○", pointer="❯", unicode=True)


def _settings(**values) -> Settings:
    base = {
        "database_url": "postgresql://x",
        "encryption_key": "a2V5",
        "api_token": "token",
        "anthropic_api_key": "sk-test",
    }
    base.update(values)
    return Settings.without_env_file(**base)


def _ctx(plan: SetupPlan | None = None, secrets: dict[str, str] | None = None) -> SetupContext:
    return SetupContext(
        plan=plan or SetupPlan(),
        secrets=dict(secrets or {}),
        ui=UI(console=Console(record=True, no_color=True), glyphs=UNICODE, color=False),
    )


def test_placeholders_and_blank_are_missing() -> None:
    for value in ("", "   ", "change-me", "CHANGEME", "your_key_here", "<your-key>", "{{token}}"):
        assert is_placeholder(value), value
    for value in ("sk-real-key", "123456:abc", "https://example.test/x"):
        assert not is_placeholder(value), value


def test_libpq_only_query_options_are_dropped() -> None:
    url = (
        "postgresql://user:pass@db.example:5432/aether"
        "?sslmode=require&channel_binding=require&connect_timeout=10&pgbouncer=true"
    )
    normalized = normalize_database_url(url)
    assert "channel_binding" not in normalized
    assert "connect_timeout" not in normalized
    assert "pgbouncer" not in normalized
    assert "sslmode=require" in normalized
    assert normalized.startswith("postgresql://user:pass@db.example:5432/aether")
    plain = "postgresql://user:pass@db.example:5432/aether"
    assert normalize_database_url(plain) == plain
    # a Unix-socket URL keeps its triple slash when a query option is dropped
    assert (
        normalize_database_url("postgresql:///aether?channel_binding=require")
        == "postgresql:///aether"
    )


def test_a_missing_driver_does_not_reject_a_correct_url(monkeypatch) -> None:
    from aether.setup import validation

    monkeypatch.setattr(validation, "_installed", lambda *modules: False)
    validation.use_network(True)
    assert validation.database_error("postgresql://u:p@127.0.0.1:1/db") is None


def test_minimal_valid_config_has_no_problems() -> None:
    assert startup_problems(_settings(), AppConfig()) == []


def test_missing_core_values_are_listed_together() -> None:
    settings = _settings(
        database_url="",
        encryption_key="",
        api_token="change-me",
        anthropic_api_key="",
    )
    joined = " ".join(startup_problems(settings, AppConfig()))
    for name in ("DATABASE_URL", "AETHER_ENCRYPTION_KEY", "AETHER_TOKEN", "ANTHROPIC_API_KEY"):
        assert name in joined


def test_memory_search_on_requires_the_gemini_key() -> None:
    config = AppConfig(llm={"embedding_model": "gemini-embedding-001"})
    joined = " ".join(startup_problems(_settings(), config))
    assert "GEMINI_API_KEY" in joined


def test_memory_search_off_needs_no_gemini_key() -> None:
    config = AppConfig(llm={"embedding_model": "off"})
    assert startup_problems(_settings(), config) == []


def test_enabled_channel_requires_its_token() -> None:
    config = AppConfig(messaging={"telegram": {"enabled": True}})
    joined = " ".join(startup_problems(_settings(), config))
    assert "TELEGRAM_BOT_TOKEN" in joined


def test_ollama_needs_no_provider_key() -> None:
    config = AppConfig(llm={"provider": "ollama", "model": "llama3"})
    settings = _settings(anthropic_api_key="")
    assert startup_problems(settings, config) == []


def test_non_interactive_reports_every_missing_answer() -> None:
    plan = SetupPlan(channels=[], semantic_memory=True)
    joined = " ".join(required_errors(_ctx(plan, {})))
    for needle in ("DATABASE_URL", "ANTHROPIC_KEY", "GEMINI_KEY", "chat channel"):
        assert needle in joined


def test_complete_answers_have_no_problems() -> None:
    plan = SetupPlan(
        channels=["telegram"],
        semantic_memory=False,
        llm_provider="anthropic",
        llm_model="claude-opus-5",
    )
    ctx = _ctx(
        plan,
        {
            "DATABASE_URL": "postgresql://x",
            "AETHER_ENCRYPTION_KEY": "a2V5",
            "AETHER_TOKEN": "token",
            "ANTHROPIC_KEY": "sk-test",
            "TELEGRAM_TOKEN": "123:abc",
        },
    )
    assert required_errors(ctx) == []


def test_render_target_requires_the_render_key() -> None:
    plan = SetupPlan(
        setup_target="render",
        channels=["telegram"],
        semantic_memory=False,
        llm_provider="ollama",
        llm_model="llama3",
    )
    ctx = _ctx(
        plan,
        {
            "DATABASE_URL": "postgresql://x",
            "AETHER_ENCRYPTION_KEY": "a2V5",
            "AETHER_TOKEN": "token",
            "TELEGRAM_TOKEN": "123:abc",
        },
    )
    assert any("RENDER_API_KEY" in problem for problem in required_errors(ctx))


def test_a_placeholder_answer_is_refused() -> None:
    plan = SetupPlan(
        channels=["telegram"],
        semantic_memory=False,
        llm_provider="anthropic",
        llm_model="claude-opus-5",
    )
    ctx = _ctx(
        plan,
        {
            "DATABASE_URL": "change-me",
            "ANTHROPIC_KEY": "sk-test",
            "TELEGRAM_TOKEN": "123:abc",
        },
    )
    assert any("DATABASE_URL" in problem for problem in required_errors(ctx))
