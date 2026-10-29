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
    # provider tokens use their own keyring so they can be rotated without
    # re-encrypting the content key's data
    token_encryption_key: str = Field(default="", validation_alias="AETHER_TOKEN_ENCRYPTION_KEY")
    token_encryption_key_id: str = Field(
        default="", validation_alias="AETHER_TOKEN_ENCRYPTION_KEY_ID"
    )
    token_encryption_key_previous: str = Field(
        default="", validation_alias="AETHER_TOKEN_ENCRYPTION_KEY_PREVIOUS"
    )
    token_encryption_key_previous_id: str = Field(
        default="", validation_alias="AETHER_TOKEN_ENCRYPTION_KEY_PREVIOUS_ID"
    )
    api_token: str = Field(default="change-me", validation_alias="AETHER_TOKEN")
    dev_ephemeral_key: bool = Field(default=False, validation_alias="AETHER_DEV_EPHEMERAL_KEY")

    database_url: str = ""
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

# The stored sentinel words for the model fields. A choice made from the
# models dropdown always carries a "/" (provider/model), so a bare word can
# never collide with a real model id. "base" is the legacy spelling of
# "default"; both keep working, and both resolve to the main model when
# providers are built — never at save time.
MODEL_BASE = "base"
MODEL_DEFAULT = "default"
MODEL_OFF = "off"


class LLMConfig(BaseModel):
    provider: Literal["anthropic", "openai", "gemini", "ollama"] = "anthropic"
    # a model value is either a sentinel word (off / default / base) or a
    # model id. An id may carry its own provider: "provider/model", split on
    # the first slash — the rest of the string is the id that provider's
    # API answers to, so an id may itself contain slashes. A bare id still
    # routes through the global provider below (the legacy spelling).
    model: str = "claude-opus-5"
    # each medium's perception, tri-state: None (default) = that medium is
    # off, "default"/"base" = the main model takes it, or a specific model
    # id. The knob is the routing truth — whether the chosen model truly
    # accepts the medium is discovered at call time, and the perception
    # ladder degrades honestly on a refusal.
    image_model: str | None = None
    audio_model: str | None = None
    video_model: str | None = None
    salience_model: str | None = None
    # the free embedding model that makes memory semantically searchable —
    # the Gemini key's own family; none (or a missing Gemini key) leaves
    # memory search unavailable
    embedding_model: str | None = "gemini-embedding-001"
    max_tokens: int = 16000
    # Ollama's vision ability depends entirely on which model was pulled;
    # the hosted providers are inherently vision-capable. Setting an
    # image_model implies this regardless.
    ollama_vision: bool = False


class AgentConfig(BaseModel):
    tick_seconds: float = 30.0
    max_tool_iterations: int = 12
    daily_surface_cap: int = 20
    # one reply's hard ceiling: a hung provider or a runaway tool loop
    # ends with a plain message to the user instead of open-ended
    # silence. Long enough that a legitimate long tool (a sandbox job
    # runs up to workbench.job_timeout_minutes) never trips it.
    turn_timeout_seconds: float = 600.0
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


def is_legacy_composio_server(name: str) -> bool:
    """The old app hub's MCP entry is ignored after the direct-provider
    cutover. It is not a user MCP server: keeping it would expose the
    broker's tools beside the native provider tools and bring the removed
    card back into the Apps page."""
    return name.strip().lower() == "composio"


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


class MediaConfig(BaseModel):
    """Shared and generated media — how much is kept, and how much is
    made. The store reads this live, so a quota change applies to the
    next stored item without a restart."""

    # how much encrypted media may sit in Postgres before the oldest
    # unreferenced items are evicted; a put with nothing left to evict
    # is refused in plain words
    storage_quota_mb: int = 200
    # who draws images: pollinations is keyless and free (no SLA),
    # gemini rides the existing AI Studio key, none turns generation off
    image_provider: Literal["pollinations", "gemini", "none"] = "pollinations"
    # the daily brakes: images made, and sandbox jobs that read or
    # edit media (frame extraction, video edits)
    max_images_per_day: int = 10
    max_sandbox_jobs_per_day: int = 20
    # image prompts travel to the image service — short by design
    max_prompt_chars: int = 500


class ProviderAppConfig(BaseModel):
    """One direct provider app's local settings."""

    poll_minutes: float = 5.0


class ProvidersConfig(BaseModel):
    """Direct provider connections. The token key is a secret and lives in
    Settings; this section carries only behavior knobs."""

    enabled: bool = True
    gmail: ProviderAppConfig = Field(default_factory=ProviderAppConfig)


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
    media: MediaConfig = Field(default_factory=MediaConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)


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
    config = AppConfig.model_validate(data)
    servers = [s for s in config.mcp_servers if not is_legacy_composio_server(s.name)]
    if len(servers) == len(config.mcp_servers):
        return config
    return config.model_copy(update={"mcp_servers": servers})
