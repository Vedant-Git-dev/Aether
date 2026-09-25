from pathlib import Path

import yaml

from aether.config import AppConfig, load_config


def test_defaults_without_file(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # no config.yaml here
    cfg = load_config()
    assert cfg.llm.provider == "anthropic"
    assert cfg.llm.model == "claude-opus-5"
    assert cfg.agent.tick_seconds == 30.0
    assert cfg.salience.threshold == 6.0
    assert cfg.mcp_servers == []
    assert cfg.contacts.mode == "off"
    assert cfg.authz.approval_ttl_hours == 24.0


def test_example_yaml_parses_and_matches_defaults() -> None:
    example = Path("config.example.yaml")
    if not example.is_file():
        example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    data = yaml.safe_load(example.read_text(encoding="utf-8"))
    cfg = AppConfig.model_validate(data)
    assert cfg.llm.provider == "anthropic"
    assert cfg.messaging.telegram.enabled is False
    assert cfg.mcp_servers == []


def test_mcp_server_config_shape() -> None:
    cfg = AppConfig.model_validate(
        {
            "mcp_servers": [
                {
                    "name": "mail",
                    "transport": {
                        "type": "stdio",
                        "command": "npx",
                        "args": ["-y", "mcp-mail-server"],
                    },
                    "poll_tools": [{"tool": "list_unread", "every_minutes": 5}],
                },
                {"name": "cal", "transport": {"type": "http", "url": "https://x/mcp"}},
            ]
        }
    )
    assert cfg.mcp_servers[0].transport.command == "npx"
    assert cfg.mcp_servers[0].poll_tools[0].every_minutes == 5.0
    assert cfg.mcp_servers[1].transport.type == "http"


def test_contacts_allowlist_shape() -> None:
    cfg = AppConfig.model_validate(
        {
            "contacts": {
                "mode": "enforce",
                "allowlist": [{"platform": "telegram", "handle": "@you"}],
            }
        }
    )
    assert cfg.contacts.mode == "enforce"
    assert cfg.contacts.allowlist[0].handle == "@you"


# ---------------------------------------------------------------------------
# Settings: every name .env.example documents must actually load
# ---------------------------------------------------------------------------


def test_settings_reads_the_documented_env_names(monkeypatch) -> None:
    from aether.config import Settings

    for name in (
        "AETHER_ENCRYPTION_KEY", "AETHER_TOKEN", "AETHER_DEV_EPHEMERAL_KEY",
        "DATABASE_URL", "PORT", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
        "GEMINI_API_KEY", "OLLAMA_BASE_URL", "TELEGRAM_BOT_TOKEN",
        "DISCORD_BOT_TOKEN", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AETHER_ENCRYPTION_KEY", "key")
    monkeypatch.setenv("AETHER_TOKEN", "tok")
    monkeypatch.setenv("AETHER_DEV_EPHEMERAL_KEY", "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("PORT", "9000")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ak")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg")

    s = Settings(_env_file=None)
    assert s.encryption_key == "key"
    assert s.api_token == "tok"
    assert s.dev_ephemeral_key is True
    assert s.database_url == "postgresql://x"
    assert s.port == 9000
    assert s.anthropic_api_key == "ak"
    assert s.telegram_bot_token == "tg"


def test_settings_env_file_loads_the_same_names(tmp_path, monkeypatch) -> None:
    from aether.config import Settings

    env = tmp_path / ".env"
    env.write_text(
        "AETHER_ENCRYPTION_KEY=filekey\nAETHER_TOKEN=filetok\nDATABASE_URL=postgresql://y\n",
        encoding="utf-8",
    )
    for name in ("AETHER_ENCRYPTION_KEY", "AETHER_TOKEN", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    s = Settings(_env_file=env)
    assert s.encryption_key == "filekey"
    assert s.api_token == "filetok"
    assert s.database_url == "postgresql://y"
