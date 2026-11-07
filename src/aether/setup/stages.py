"""The ordered question flow.

Each question is a :class:`Stage`: it knows how to run interactively, how to
fill itself in non-interactive mode, when it applies, and how to clear itself
when the user goes back. The wizard walks the list and calls a stage only when
it applies, so dependent questions (image provider, fork URL, uptime email)
appear exactly when the earlier answers make them relevant.

Copy rule: a question carries a prompt and at most a one-line hint. Results
and errors say what happened and what to do; nothing announces a decision or
explains why a feature exists.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..config_validation import is_missing, is_placeholder
from . import catalogs
from .links import open_hint, open_link
from .plan import SetupPlan
from .prompts import UI, ask_secret, ask_text, select_many, select_one
from .validation import (
    database_error,
    discord_token_error,
    embedding_key_error,
    model_key_error,
    render_api_key_error,
    render_service_name_error,
    telegram_token_error,
)

PROVIDER_KEYS = {
    "anthropic": ("ANTHROPIC_KEY", "Anthropic API key"),
    "openai": ("OPENAI_KEY", "OpenAI API key"),
    "gemini": ("GEMINI_KEY", "Gemini API key"),
    "nvidia": ("NVIDIA_KEY", "NVIDIA API key"),
}
PROVIDER_LINKS = {
    "anthropic": "https://console.anthropic.com/settings/keys",
    "openai": "https://platform.openai.com/api-keys",
    "gemini": "https://aistudio.google.com/apikey",
    "nvidia": "https://build.nvidia.com/",
}
GOOGLE_APPS = {"gmail", "calendar", "drive", "docs"}
PICKER_APPS = {"drive", "docs"}

EMBEDDING_MODEL = "gemini-embedding-001"


@dataclass
class SetupContext:
    plan: SetupPlan
    secrets: dict[str, str]
    ui: UI
    root: Path | None = None
    locked_secrets: set[str] = field(default_factory=set)
    problems: list[str] = field(default_factory=list)

    def say(self, text: str = "") -> None:
        self.ui.console.print(text)

    def has_service(self, needle: str) -> bool:
        lowered = needle.lower()
        return any(lowered in item.lower() for item in self.plan.services)

    def has_app(self, key: str) -> bool:
        return key in self.plan.apps

    def has_channel(self, key: str) -> bool:
        return key in self.plan.channels

    def secret(self, key: str) -> str:
        return self.secrets.get(key, "")


@dataclass
class Stage:
    key: str
    run: Callable[[SetupContext], None]
    autofill: Callable[[SetupContext], None]
    visible: Callable[[SetupContext], bool] = lambda ctx: True
    reset: Callable[[SetupContext], None] = lambda ctx: None


def _noop(ctx: SetupContext) -> None:
    return None


def _always(ctx: SetupContext) -> bool:
    return True


def _index_of(keys: list[str], value: str) -> int | None:
    try:
        return keys.index(value)
    except ValueError:
        return None


def choice_stage(
    name: str,
    question: str,
    choices: list[catalogs.Choice],
    *,
    attribute: str,
    default: int = 0,
    hint: str = "",
    visible: Callable[[SetupContext], bool] = _always,
) -> Stage:
    keys = [choice.key for choice in choices]
    labels = [choice.label for choice in choices]

    def run(ctx: SetupContext) -> None:
        if hint:
            ctx.say(hint)
        current = getattr(ctx.plan, attribute)
        found = _index_of(keys, current)
        picked = select_one(
            ctx.ui, question, labels, default_index=found if found is not None else default
        )
        setattr(ctx.plan, attribute, keys[picked])

    def autofill(ctx: SetupContext) -> None:
        if getattr(ctx.plan, attribute) not in keys:
            setattr(ctx.plan, attribute, keys[default])

    return Stage(name, run, autofill, visible)


def multi_stage(
    name: str,
    question: str,
    choices: list[catalogs.Choice],
    *,
    attribute: str,
    store: str = "key",
    min_selected: int = 0,
    hint: str = "",
) -> Stage:
    labels = [choice.label for choice in choices]
    values = [(choice.label if store == "label" else choice.key) for choice in choices]

    def run(ctx: SetupContext) -> None:
        if hint:
            ctx.say(hint)
        current = set(getattr(ctx.plan, attribute))
        selected = {index for index, value in enumerate(values) if value in current}
        while True:
            picked = select_many(ctx.ui, question, labels, default_selected=selected)
            chosen = [values[index] for index in picked]
            if len(chosen) >= min_selected:
                setattr(ctx.plan, attribute, chosen)
                return
            noun = "one" if min_selected == 1 else str(min_selected)
            ctx.say(f"  choose at least {noun}.")

    def autofill(ctx: SetupContext) -> None:
        # Keep whatever the answers file or the defaults set.
        current = getattr(ctx.plan, attribute)
        setattr(ctx.plan, attribute, [value for value in current if value in values])

    return Stage(name, run, autofill)


def text_stage(
    name: str,
    question: str,
    *,
    attribute: str,
    default: Callable[[SetupContext], str] | str = "",
    required: bool = False,
    validate: Callable[[str], str | None] | None = None,
    visible: Callable[[SetupContext], bool] = _always,
) -> Stage:
    def default_value(ctx: SetupContext) -> str:
        return default(ctx) if callable(default) else default

    def run(ctx: SetupContext) -> None:
        while True:
            current = getattr(ctx.plan, attribute) or default_value(ctx)
            value = ask_text(ctx.ui, question, default=current)
            if is_missing(value):
                if not required:
                    setattr(ctx.plan, attribute, "")
                    return
                ctx.say("  this value is required.")
                continue
            if is_placeholder(value):
                ctx.say("  that looks like a placeholder — enter the real value.")
                continue
            if validate is not None:
                error = validate(value)
                if error:
                    ctx.say(f"  {error}")
                    continue
            setattr(ctx.plan, attribute, value)
            return

    def autofill(ctx: SetupContext) -> None:
        if not getattr(ctx.plan, attribute):
            setattr(ctx.plan, attribute, default_value(ctx))

    return Stage(name, run, autofill, visible)


def secret_stage(
    name: str,
    secret_key: str,
    question: str,
    *,
    visible: Callable[[SetupContext], bool] = _always,
    hint: str = "",
    required: bool = False,
    validate: Callable[[str], str | None] | None = None,
) -> Stage:
    def applies(ctx: SetupContext) -> bool:
        if secret_key in ctx.locked_secrets:
            return False
        return visible(ctx)

    def run(ctx: SetupContext) -> None:
        if hint:
            ctx.say(hint)
            open_hint(hint)
            ctx.say("")
        while True:
            value = ask_secret(ctx.ui, question).strip()
            if not value:
                if not required:
                    ctx.say(f"  skipped {secret_key}")
                    return
                ctx.say(f"  {secret_key} is required — paste it, or Ctrl+C to stop.")
                continue
            if is_placeholder(value):
                ctx.say("  that looks like a placeholder — paste the real value.")
                continue
            if validate is not None:
                error = validate(value)
                if error:
                    ctx.say(f"  {error}")
                    continue
            ctx.secrets[secret_key] = value
            ctx.say(f"  stored {secret_key} (masked)")
            return

    def autofill(ctx: SetupContext) -> None:
        if required and is_missing(ctx.secret(secret_key)):
            ctx.problems.append(f"{secret_key} is required")

    return Stage(name, run, autofill, applies)


def _yes_no(name: str, question: str, *, attribute: str, hint: str = "") -> Stage:
    def run(ctx: SetupContext) -> None:
        if hint:
            ctx.say(hint)
        current = bool(getattr(ctx.plan, attribute))
        picked = select_one(ctx.ui, question, ["No", "Yes"], default_index=1 if current else 0)
        setattr(ctx.plan, attribute, bool(picked))

    def autofill(ctx: SetupContext) -> None:
        return None

    return Stage(name, run, autofill)


def provider_stage() -> Stage:
    names = catalogs.providers()

    def run(ctx: SetupContext) -> None:
        current = ctx.plan.llm_provider
        found = _index_of(names, current)
        picked = select_one(
            ctx.ui, "Main model provider", names, default_index=found if found is not None else 0
        )
        chosen = names[picked]
        if chosen != current:
            ctx.plan.llm_model = ""
        ctx.plan.llm_provider = chosen

    def autofill(ctx: SetupContext) -> None:
        if ctx.plan.llm_provider not in names:
            ctx.plan.llm_provider = names[0]

    return Stage("provider", run, autofill)


def _state_choices(items: list[tuple[str, str]]) -> list[catalogs.Choice]:
    return [catalogs.Choice(key, label) for key, label in items]


def _has_image(ctx: SetupContext) -> bool:
    return ctx.has_service("image generation")


def _has_jobs(ctx: SetupContext) -> bool:
    return ctx.has_service("background jobs")


def _has_google(ctx: SetupContext) -> bool:
    return any(ctx.has_app(key) for key in GOOGLE_APPS)


def _has_picker(ctx: SetupContext) -> bool:
    return any(ctx.has_app(key) for key in PICKER_APPS)


def _provider_key_stage() -> Stage:
    def applies(ctx: SetupContext) -> bool:
        entry = PROVIDER_KEYS.get(ctx.plan.llm_provider)
        if entry is None:  # Ollama has no key; its host is checked at the end
            return False
        return entry[0] not in ctx.locked_secrets and not ctx.secret(entry[0])

    def run(ctx: SetupContext) -> None:
        entry = PROVIDER_KEYS.get(ctx.plan.llm_provider)
        if entry is None:
            return
        key, label = entry
        link = PROVIDER_LINKS.get(ctx.plan.llm_provider, "")
        if link:
            ctx.say(f"  Create it here: {link}")
            open_link(link)
        while True:
            value = ask_secret(ctx.ui, label).strip()
            if is_missing(value):
                ctx.say(f"  {label} is required — paste it, or Ctrl+C to stop.")
                continue
            if is_placeholder(value):
                ctx.say("  that looks like a placeholder — paste the real value.")
                continue
            error = model_key_error(ctx.plan.llm_provider, value)
            if error:
                ctx.say(f"  {error}")
                continue
            ctx.secrets[key] = value
            ctx.say(f"  stored {key} (masked)")
            return

    def autofill(ctx: SetupContext) -> None:
        entry = PROVIDER_KEYS.get(ctx.plan.llm_provider)
        if entry and is_missing(ctx.secret(entry[0])):
            ctx.problems.append(f"{entry[0]} is required for {ctx.plan.llm_provider}")

    return Stage("provider_key", run, autofill, applies)


def _gemini_stage() -> Stage:
    def applies(ctx: SetupContext) -> bool:
        return (
            ctx.plan.semantic_memory
            and "GEMINI_KEY" not in ctx.locked_secrets
            and not ctx.secret("GEMINI_KEY")
        )

    def run(ctx: SetupContext) -> None:
        ctx.say("  Create a key here: https://aistudio.google.com/apikey")
        open_link("https://aistudio.google.com/apikey")
        while True:
            value = ask_secret(ctx.ui, "Gemini API key").strip()
            if is_missing(value):
                ctx.say("  a Gemini API key is required while memory search is on.")
                continue
            if is_placeholder(value):
                ctx.say("  that looks like a placeholder — paste the real value.")
                continue
            error = embedding_key_error(value, EMBEDDING_MODEL)
            if error:
                ctx.say(f"  {error}")
                continue
            ctx.secrets["GEMINI_KEY"] = value
            ctx.say("  stored GEMINI_KEY (masked)")
            return

    def autofill(ctx: SetupContext) -> None:
        if ctx.plan.semantic_memory and is_missing(ctx.secret("GEMINI_KEY")):
            ctx.problems.append("GEMINI_KEY is required while memory search is on")

    return Stage("gemini_key", run, autofill, applies)


def _workbench_secret_stages() -> list[Stage]:
    def modal(ctx: SetupContext) -> bool:
        return ctx.plan.workbench_provider == "modal"

    def vercel(ctx: SetupContext) -> bool:
        return ctx.plan.workbench_provider == "vercel"

    return [
        secret_stage(
            "modal_token_id",
            "MODAL_TOKEN_ID",
            "Modal token ID",
            visible=modal,
            hint="  https://modal.com/settings/tokens",
            required=True,
        ),
        secret_stage(
            "modal_token_secret",
            "MODAL_TOKEN_SECRET",
            "Modal token secret",
            visible=modal,
            required=True,
        ),
        secret_stage(
            "vercel_token",
            "VERCEL_TOKEN",
            "Vercel token",
            visible=vercel,
            hint="  https://vercel.com/account/tokens",
            required=True,
        ),
        secret_stage(
            "vercel_team_id",
            "VERCEL_TEAM_ID",
            "Vercel team id",
            visible=vercel,
            required=True,
        ),
        secret_stage(
            "vercel_project_id",
            "VERCEL_PROJECT_ID",
            "Vercel project id",
            visible=vercel,
            required=True,
        ),
        secret_stage(
            "github_actions_token",
            "GITHUB_ACTIONS_TOKEN",
            "GitHub Actions token",
            visible=lambda c: c.plan.workbench_provider == "github_actions",
            hint="  https://github.com/settings/tokens",
            required=True,
        ),
    ]


def _google_stages() -> list[Stage]:
    return [
        secret_stage(
            "google_client_id",
            "GOOGLE_CLIENT_ID",
            "Google OAuth client ID",
            visible=_has_google,
            hint="  https://console.cloud.google.com/apis/credentials",
            required=True,
        ),
        secret_stage(
            "google_client_secret",
            "GOOGLE_CLIENT_SECRET",
            "Google OAuth client secret",
            visible=_has_google,
            required=True,
        ),
        secret_stage(
            "google_picker_key",
            "GOOGLE_PICKER_KEY",
            "Google Picker API key",
            visible=_has_picker,
            required=True,
        ),
        secret_stage(
            "google_picker_app_id",
            "GOOGLE_PICKER_APP_ID",
            "Google Picker app id",
            visible=_has_picker,
            required=True,
        ),
    ]


def _uptime_stage() -> Stage:
    """On/off for the deployed instance's keep-awake monitor, plus its email."""

    def run(ctx: SetupContext) -> None:
        on = select_one(
            ctx.ui,
            "Keep the deployed instance awake with uptime monitoring?",
            ["No", "Yes"],
            default_index=0,
        )
        ctx.plan.uptime_mode = "email" if on == 1 else "skip"
        if on == 0:
            ctx.plan.alert_email = ""

    def autofill(ctx: SetupContext) -> None:
        if ctx.plan.uptime_mode not in {"email", "skip"}:
            ctx.plan.uptime_mode = "skip"

    return Stage("uptime", run, autofill, visible=lambda c: c.plan.setup_target == "render")


def _render_name_validator(ctx: SetupContext):
    """Say the check is running, then run it on the entered name."""

    def validate(value: str) -> str | None:
        ctx.say(f"  Checking {value} on Render...")
        return render_service_name_error(value, ctx.secret("RENDER_API_KEY"))

    return validate


def _alert_email_stage() -> Stage:
    return text_stage(
        "alert_email",
        "Alert email address",
        attribute="alert_email",
        required=True,
        visible=lambda c: c.plan.setup_target == "render" and c.plan.uptime_mode == "email",
    )


def build_flow(ctx: SetupContext) -> list[Stage]:
    """The whole ordered flow, with dependent stages included."""
    services = catalogs.optional_services(ctx.root)
    apps = catalogs.direct_apps(ctx.root)
    channels = catalogs.chat_channels()
    render = lambda c: c.plan.setup_target == "render"  # noqa: E731

    flow: list[Stage] = [
        choice_stage(
            "setup_target",
            "Where should Aether run?",
            _state_choices(catalogs.SETUP_TARGETS),
            attribute="setup_target",
        ),
        choice_stage(
            "database",
            "Database",
            _state_choices(catalogs.DATABASES),
            attribute="database_mode",
            hint="  Neon or any Postgres; keep sslmode=require.",
        ),
        provider_stage(),
        text_stage(
            "model",
            "Main model",
            attribute="llm_model",
            default=lambda c: catalogs.example_model(c.plan.llm_provider, c.root),
            required=True,
        ),
        _yes_no(
            "semantic_memory",
            "Semantic memory (Gemini embeddings)?",
            attribute="semantic_memory",
        ),
        multi_stage(
            "services",
            "Optional services",
            services,
            attribute="services",
            store="label",
        ),
        choice_stage(
            "image_provider",
            "Image provider",
            _state_choices(catalogs.IMAGE_PROVIDERS),
            attribute="image_provider",
            visible=_has_image,
        ),
        choice_stage(
            "workbench_provider",
            "Background-work sandbox",
            _state_choices(catalogs.WORKBENCH_PROVIDERS),
            attribute="workbench_provider",
            visible=_has_jobs,
        ),
        multi_stage("apps", "Direct apps", apps, attribute="apps"),
        multi_stage(
            "channels",
            "Chat channels (choose at least one)",
            channels,
            attribute="channels",
            min_selected=1,
        ),
        choice_stage(
            "plan",
            "Render plan",
            _state_choices(catalogs.PLANS),
            attribute="plan",
            visible=render,
        ),
        secret_stage(
            "render_api_key",
            "RENDER_API_KEY",
            "Render API key",
            visible=render,
            hint="  https://dashboard.render.com/u/settings#api-keys",
            required=True,
            validate=render_api_key_error,
        ),
        text_stage(
            "service_name",
            "Render service name",
            attribute="service_name",
            default="aether",
            visible=render,
            required=True,
            validate=_render_name_validator(ctx),
        ),
        choice_stage(
            "region",
            "Render region",
            _state_choices([(region, region) for region in catalogs.REGIONS]),
            attribute="region",
            visible=render,
        ),
        choice_stage(
            "deploy_source",
            "Deploy source",
            _state_choices(catalogs.DEPLOY_SOURCES),
            attribute="deploy_source",
            visible=render,
        ),
        text_stage(
            "repo_url",
            "Your fork URL",
            attribute="repo_url",
            visible=lambda c: render(c) and c.plan.deploy_source == "fork",
            required=True,
            validate=lambda value: (
                None
                if value.startswith("https://github.com/") and "CHANGE_ME" not in value
                else "use your fork's https://github.com/<owner>/<repo> URL."
            ),
        ),
        _uptime_stage(),
        _alert_email_stage(),
        secret_stage(
            "database_url",
            "DATABASE_URL",
            "Postgres connection string (keep sslmode=require)",
            hint="  Paste the connection string from Neon, Render, or your own server.",
            required=True,
            validate=database_error,
        ),
        _provider_key_stage(),
        secret_stage(
            "tavily_key",
            "TAVILY_KEY",
            "Tavily API key",
            visible=lambda c: c.has_service("web search"),
            hint="  https://app.tavily.com",
            required=True,
        ),
        secret_stage(
            "groq_key",
            "GROQ_KEY",
            "Groq API key",
            visible=lambda c: c.has_service("voice-note transcription"),
            hint="  https://console.groq.com/keys",
            required=True,
        ),
        _gemini_stage(),
        *_workbench_secret_stages(),
        *_google_stages(),
        secret_stage(
            "linkedin_client_id",
            "LINKEDIN_CLIENT_ID",
            "LinkedIn client ID",
            visible=lambda c: c.has_app("linkedin"),
            hint="  https://www.linkedin.com/developers/apps",
            required=True,
        ),
        secret_stage(
            "linkedin_client_secret",
            "LINKEDIN_CLIENT_SECRET",
            "LinkedIn client secret",
            visible=lambda c: c.has_app("linkedin"),
            required=True,
        ),
        secret_stage(
            "telegram_token",
            "TELEGRAM_TOKEN",
            "Telegram bot token",
            visible=lambda c: c.has_channel("telegram"),
            hint="  https://t.me/BotFather",
            required=True,
            validate=telegram_token_error,
        ),
        secret_stage(
            "discord_token",
            "DISCORD_TOKEN",
            "Discord bot token",
            visible=lambda c: c.has_channel("discord"),
            hint="  https://discord.com/developers/applications",
            required=True,
            validate=discord_token_error,
        ),
    ]
    return flow
