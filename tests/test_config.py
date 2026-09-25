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
