"""Configuration: secrets via environment/.env, structure via config.yaml."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Secrets, loaded from the environment (and an optional .env file).

    Aether-specific secrets carry the AETHER_ prefix; everything shared
    keeps the name its platform already uses (DATABASE_URL, PORT,
    <PROVIDER>_API_KEY, <PLATFORM>_BOT_TOKEN).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,  # tests construct Settings(api_token=...) by name
    )

    encryption_key: str = Field(default="", validation_alias="AETHER_ENCRYPTION_KEY")
    api_token: str = Field(default="change-me", validation_alias="AETHER_TOKEN")
    dev_ephemeral_key: bool = Field(default=False, validation_alias="AETHER_DEV_EPHEMERAL_KEY")

    database_url: str = ""
    port: int = 8000

    # LLM provider credentials
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434/v1"
    nvidia_api_key: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"

    # Messaging connectors
    telegram_bot_token: str = ""
    discord_bot_token: str = ""
    slack_bot_token: str = ""
    slack_app_token: str = ""

    # Composio — the one connection hub for external apps (gmail, github,
    # notion, ...). Boot reads this; the /apps walk's paste lands in the
    # encrypted store and the bridge resolves it fresh from there.
    composio_api_key: str = ""

    # this deployment's public address (https://your-app.onrender.com) —
    # where externally-hosted flows would call back; reserved for a future
    # webhook ingress, nothing reads it today
    public_url: str = Field(default="", validation_alias="AETHER_PUBLIC_URL")

    log_level: str = "INFO"

    @classmethod
    def without_env_file(cls, **values: Any) -> Settings:
        """Settings with the optional .env file deliberately not read — the
        construction path tests and the config resolver take, so a real
        .env sitting next to them can't leak into the values. (`_env_file`
        is a real pydantic-settings argument, but the type checkers can't
        see it — the one ignore lives here, once, not at every call site.)"""
        return cls(_env_file=None, **values)  # type: ignore[call-arg]

    @classmethod
    def from_env_file(cls, env_file: str | Path, **values: Any) -> Settings:
        """Settings with one specific .env file — the deliberate other
        direction, for the tests that exercise the env-file path itself."""
        return cls(_env_file=env_file, **values)  # type: ignore[call-arg]


# --------------------------------------------------------------------------
# config.yaml structure models
# --------------------------------------------------------------------------


class LLMConfig(BaseModel):
    provider: Literal["anthropic", "openai", "gemini", "ollama", "nvidia"] = "anthropic"
    model: str = "claude-opus-5"
    vision_model: str | None = None
    salience_model: str | None = None
    # the free embedding model that makes memory semantically searchable;
    # none (or a missing Gemini key) leaves memory search unavailable
    embedding_model: str | None = "gemini-embedding-001"
    max_tokens: int = 16000
    # Ollama's vision ability depends entirely on which model was pulled;
    # the hosted providers are inherently vision-capable. Setting a
    # vision_model implies this regardless.
    ollama_vision: bool = False
    # NIM hosts both text-only and vision-capable models under one API —
    # same ambiguity as Ollama, same fix: a config flag names the choice.
    nvidia_vision: bool = False


class AgentConfig(BaseModel):
    tick_seconds: float = 30.0
    max_tool_iterations: int = 12
    daily_surface_cap: int = 20
    # the web search tool's own brake — shared by chat and the background
    # worker, counted per day, so the free search allowance can't be
    # burned in one afternoon
    max_searches_per_day: int = 25
    # the interruption budget: unprompted commentary waits out this window
    # (local time, wraps midnight) unless an observation scores at/above the
    # urgent threshold. Replies to the user never wait.
    quiet_hours: str | None = None  # "23:00-08:00"
    quiet_urgent_salience: float = 8.0  # 0-10, only during the window


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


class AppContacts(BaseModel):
    """One app's own slice of the allowlist: its list of people, and a
    switch that either overrides the default or follows it (None)."""

    mode: Literal["enforce", "off"] | None = None

    @field_validator("mode", mode="before")
    @classmethod
    def _coerce_yaml_bool(cls, v: object) -> object:
        # the same YAML 1.1 bare-boolean trap as the section default
        if v is False:
            return "off"
        if v is True:
            return "enforce"
        return v

    allowlist: list[ContactRule] = Field(default_factory=list)


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

    # the cross-app ("Any platform") bucket — a rule here counts everywhere
    allowlist: list[ContactRule] = Field(default_factory=list)

    # each connected app's own people, consulted alongside the bucket above;
    # a None mode means the app follows the section default
    gmail: AppContacts = Field(default_factory=AppContacts)
    telegram: AppContacts = Field(default_factory=AppContacts)
    discord: AppContacts = Field(default_factory=AppContacts)
    slack: AppContacts = Field(default_factory=AppContacts)


class AuthzRule(BaseModel):
    tool_pattern: str
    param_pattern: str | None = None
    decision: Literal["allow", "approve", "deny"]
    note: str = ""


class AuthzConfig(BaseModel):
    rules: list[AuthzRule] = Field(default_factory=list)
    approval_ttl_hours: float = 24.0


class WorkspaceConfig(BaseModel):
    """The human-editable Markdown workspace (identity, personality, user
    preferences, long-term and daily memory) — separate from, and much
    smaller than, the Postgres event memory."""

    enabled: bool = True
    path: str = "aether-workspace"
    # AES-256-GCM at rest with the server's key. Turning this off later
    # makes new files plain text again but never decrypts what was
    # already written — those keep reading as empty until re-created.
    encrypted: bool = True


class ProactiveConfig(BaseModel):
    """The background worker — off by default, every cap a hard stop.
    The worker reads this section live on every cycle, so a change made
    in chat or the panel applies from the next cycle without a restart."""

    enabled: bool = False
    # how often the worker looks around; a cycle that finds nothing
    # costs nothing but the look
    interval_minutes: float = 30.0
    # the daily brakes: tasks claimed, LLM tokens, sandbox time
    max_tasks_per_day: int = 3
    max_tokens_per_day: int = 100_000
    max_sandbox_minutes_per_day: int = 20
    # the notification brake for results (quiet hours ride their own budget)
    notify_daily_cap: int = 5
    # which skills the worker may run; names not in the registry are ignored
    skills: list[str] = Field(default_factory=lambda: ["followups", "research", "media"])


class WorkbenchConfig(BaseModel):
    """Where background jobs run when they must not run on this machine.
    The provider is the configured default; the routing ladder falls
    through to the next available one on a dispatch failure."""

    provider: Literal["modal", "vercel", "github_actions", "none"] = "modal"
    # the batch queue bills wall-clock minutes — this month's ceiling
    monthly_minutes_cap: int = 1500
    # one job's own timeout; a job that exceeds it reads as a real
    # timed-out result, never a fallback
    job_timeout_minutes: int = 10
    # the batch queue's own home: the repository that carries the runner
    # workflow, and the branch it dispatches on. Empty means the batch
    # provider stays unconstructed — the ladder simply never sees it.
    actions_repo: str = ""
    actions_workflow: str = ""
    actions_ref: str = "main"


class AppConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    salience: SalienceConfig = Field(default_factory=SalienceConfig)
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)
    messaging: MessagingConfig = Field(default_factory=MessagingConfig)
    contacts: ContactsConfig = Field(default_factory=ContactsConfig)
    authz: AuthzConfig = Field(default_factory=AuthzConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    proactive: ProactiveConfig = Field(default_factory=ProactiveConfig)
    workbench: WorkbenchConfig = Field(default_factory=WorkbenchConfig)


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load config.yaml from an explicit path, $AETHER_CONFIG, or ./config.yaml.

    Missing file → all defaults (the agent still runs, with no connectors).
    """
    if path is None:
        env_path = os.environ.get("AETHER_CONFIG")
        path = Path(env_path) if env_path else Path("config.yaml")
    p = Path(path)
    if not p.is_file():
        return AppConfig()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(data)
