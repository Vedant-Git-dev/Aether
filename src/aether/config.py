"""Configuration: secrets via environment/.env, structure via config.yaml."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Secrets, loaded from the environment (and an optional .env file)."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = ""
    encryption_key: str = ""
    api_token: str = "change-me"
    port: int = 8000

    # LLM provider credentials
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434/v1"

    # Messaging connectors
    telegram_bot_token: str = ""
    discord_bot_token: str = ""
    slack_bot_token: str = ""
    slack_app_token: str = ""

    # Dev convenience
    dev_ephemeral_key: bool = False
    log_level: str = "INFO"


# --------------------------------------------------------------------------
# config.yaml structure models
# --------------------------------------------------------------------------


class LLMConfig(BaseModel):
    provider: Literal["anthropic", "openai", "gemini", "ollama"] = "anthropic"
    model: str = "claude-opus-5"
    vision_model: str | None = None
    salience_model: str | None = None
    max_tokens: int = 16000


class AgentConfig(BaseModel):
    tick_seconds: float = 30.0
    max_tool_iterations: int = 12
    daily_surface_cap: int = 20


class SalienceConfig(BaseModel):
    threshold: float = 6.0
    rate_cap_per_hour: int = 20


class TransportConfig(BaseModel):
    type: Literal["stdio", "http"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)


class PollTool(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    every_minutes: float = 5.0


class MCPServerConfig(BaseModel):
    name: str
    transport: TransportConfig = Field(default_factory=TransportConfig)
    poll_tools: list[PollTool] = Field(default_factory=list)
    enabled: bool = True


class PlatformToggle(BaseModel):
    enabled: bool = False


class MessagingConfig(BaseModel):
    telegram: PlatformToggle = Field(default_factory=PlatformToggle)
    discord: PlatformToggle = Field(default_factory=PlatformToggle)
    slack: PlatformToggle = Field(default_factory=PlatformToggle)


class ContactRule(BaseModel):
    platform: str = "*"
    handle: str


class ContactsConfig(BaseModel):
    # YAML 1.1 parses bare `off`/`on` as booleans — coerce the traps so both
    # spellings of a user's config.yaml work.
    mode: Literal["enforce", "off"] = "off"

    @field_validator("mode", mode="before")
    @classmethod
    def _coerce_yaml_bool(cls, v: object) -> object:
        if v is False:
            return "off"
        if v is True:
            return "enforce"
        return v

    allowlist: list[ContactRule] = Field(default_factory=list)


class AuthzRule(BaseModel):
    tool_pattern: str
    param_pattern: str | None = None
    decision: Literal["allow", "approve", "deny"]
    note: str = ""


class AuthzConfig(BaseModel):
    rules: list[AuthzRule] = Field(default_factory=list)
    approval_ttl_hours: float = 24.0


class AppConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    salience: SalienceConfig = Field(default_factory=SalienceConfig)
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)
    messaging: MessagingConfig = Field(default_factory=MessagingConfig)
    contacts: ContactsConfig = Field(default_factory=ContactsConfig)
    authz: AuthzConfig = Field(default_factory=AuthzConfig)


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load config.yaml from an explicit path, $AETHER_CONFIG, or ./config.yaml.

    Missing file → all defaults (the agent still runs, with no connectors).
    """
    if path is None:
        env_path = os.environ.get("AETHER_CONFIG")
        if env_path:
            path = Path(env_path)
        else:
            path = Path("config.yaml")
    p = Path(path)
    if not p.is_file():
        return AppConfig()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(data)
