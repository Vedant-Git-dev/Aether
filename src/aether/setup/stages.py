"""The ordered question flow.

Each question is a :class:`Stage`: it knows how to run interactively, how to
fill itself in non-interactive mode, when it applies, and how to clear itself
when the user goes back. The wizard walks the list and calls a stage only when
it applies, so dependent questions (image provider, fork URL, uptime email)
appear exactly when the earlier answers make them relevant.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import catalogs
from .plan import SetupPlan
from .prompts import UI, ask_secret, ask_text, select_many, select_one

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


@dataclass
class SetupContext:
    plan: SetupPlan
    secrets: dict[str, str]
    ui: UI
    root: Path | None = None
    locked_secrets: set[str] = field(default_factory=set)

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
    note: str = "",
    visible: Callable[[SetupContext], bool] = _always,
) -> Stage:
    keys = [choice.key for choice in choices]
    labels = [choice.label for choice in choices]

    def run(ctx: SetupContext) -> None:
        if note:
            ctx.say(note)
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
    note: str = "",
) -> Stage:
    labels = [choice.label for choice in choices]
    values = [(choice.label if store == "label" else choice.key) for choice in choices]

    def run(ctx: SetupContext) -> None:
        if note:
            ctx.say(note)
        current = set(getattr(ctx.plan, attribute))
        selected = {index for index, value in enumerate(values) if value in current}
        picked = select_many(ctx.ui, question, labels, default_selected=selected)
        setattr(ctx.plan, attribute, [values[index] for index in picked])

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
    visible: Callable[[SetupContext], bool] = _always,
) -> Stage:
    def default_value(ctx: SetupContext) -> str:
        return default(ctx) if callable(default) else default

    def run(ctx: SetupContext) -> None:
        current = getattr(ctx.plan, attribute) or default_value(ctx)
        setattr(ctx.plan, attribute, ask_text(ctx.ui, question, default=current))

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
) -> Stage:
    def applies(ctx: SetupContext) -> bool:
        if secret_key in ctx.locked_secrets:
            return False
        return visible(ctx)

    def run(ctx: SetupContext) -> None:
        if hint:
            ctx.say(hint)
            ctx.say("")
        while True:
            value = ask_secret(ctx.ui, question)
            if value:
                ctx.secrets[secret_key] = value
                ctx.say(f"  stored {secret_key} (masked)")
                return
            if not required:
                ctx.say(f"  skipped {secret_key}")
                return
            ctx.say(f"  {secret_key} is required; paste it, or press Esc to go back")

    def autofill(ctx: SetupContext) -> None:
        if required and not ctx.secret(secret_key):
            ctx.say(f"  warning: {secret_key} is not set")

    return Stage(name, run, autofill, applies)


def _yes_no(name: str, question: str, *, attribute: str) -> Stage:
    def run(ctx: SetupContext) -> None:
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
        if entry is None:
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
        value = ask_secret(ctx.ui, label)
        if value:
            ctx.secrets[key] = value
            ctx.say(f"  stored {key} (masked)")
        else:
            ctx.say(f"  skipped {key}")

    def autofill(ctx: SetupContext) -> None:
        entry = PROVIDER_KEYS.get(ctx.plan.llm_provider)
        if entry and not ctx.secret(entry[0]):
            ctx.say(f"  warning: {entry[0]} is not set")

    return Stage("provider_key", run, autofill, applies)


def _gemini_stage() -> Stage:
    def applies(ctx: SetupContext) -> bool:
        return "GEMINI_KEY" not in ctx.locked_secrets and not ctx.secret("GEMINI_KEY")

    def run(ctx: SetupContext) -> None:
        ctx.say("  Semantic memory is always on and uses Gemini embeddings.")
        ctx.say("  Create a key here: https://aistudio.google.com/apikey")
        value = ask_secret(ctx.ui, "Gemini API key (semantic memory)")
        if value:
            ctx.secrets["GEMINI_KEY"] = value
            ctx.say("  stored GEMINI_KEY (masked)")
        else:
            ctx.say("  skipped GEMINI_KEY (memory search will stay off)")

    def autofill(ctx: SetupContext) -> None:
        if not ctx.secret("GEMINI_KEY"):
            ctx.say("  warning: GEMINI_KEY is not set (semantic memory will stay off)")

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
            hint="  Create a token pair at https://modal.com/settings/tokens",
        ),
        secret_stage(
            "modal_token_secret",
            "MODAL_TOKEN_SECRET",
            "Modal token secret",
            visible=modal,
        ),
        secret_stage(
            "vercel_token",
            "VERCEL_TOKEN",
            "Vercel token",
            visible=vercel,
            hint="  Create a token at https://vercel.com/account/tokens",
        ),
    ]


def _google_stages() -> list[Stage]:
    return [
        secret_stage(
            "google_client_id",
            "GOOGLE_CLIENT_ID",
            "Google OAuth client ID",
            visible=_has_google,
            hint=(
                "  Google direct apps need an OAuth client you own.\n"
                "  https://console.cloud.google.com/apis/credentials"
            ),
        ),
        secret_stage(
            "google_client_secret",
            "GOOGLE_CLIENT_SECRET",
            "Google OAuth client secret",
            visible=_has_google,
        ),
        secret_stage(
            "google_picker_key",
            "GOOGLE_PICKER_KEY",
            "Google Picker API key",
            visible=_has_picker,
        ),
        secret_stage(
            "google_picker_app_id",
            "GOOGLE_PICKER_APP_ID",
            "Google Picker app id",
            visible=_has_picker,
        ),
    ]


def build_flow(ctx: SetupContext) -> list[Stage]:
    """The whole ordered flow, with dependent stages included."""
    services = catalogs.optional_services(ctx.root)
    apps = catalogs.direct_apps(ctx.root)
    channels = catalogs.chat_channels()

    flow: list[Stage] = [
        choice_stage(
            "database",
            "Database",
            _state_choices(catalogs.DATABASES),
            attribute="database_mode",
            note=(
                "Neon's free tier does not expire; Render's free Postgres expires\n"
                "30 days after creation. An existing database needs a connection\n"
                "string with sslmode=require."
            ),
        ),
        provider_stage(),
        text_stage(
            "model",
            "Main model",
            attribute="llm_model",
            default=lambda c: catalogs.example_model(c.plan.llm_provider, c.root),
        ),
        multi_stage(
            "services",
            "Optional services",
            services,
            attribute="services",
            store="label",
            note="  Semantic memory is always on and is not an optional choice.",
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
        multi_stage(
            "apps",
            "Direct apps",
            apps,
            attribute="apps",
            note="  Direct apps use a sign-in you own; the token keyring is generated for you.",
        ),
        multi_stage("channels", "Chat channels", channels, attribute="channels"),
        choice_stage(
            "plan",
            "Render plan",
            _state_choices(catalogs.PLANS),
            attribute="plan",
            note=(
                "Free: 512 MB, spins down after 15 idle minutes, ephemeral disk.\n"
                "Starter: about $7/month, no spin-down, 1 GB persistent disk."
            ),
        ),
        text_stage(
            "service_name", "Render service name", attribute="service_name", default="aether"
        ),
        choice_stage(
            "region",
            "Render region",
            _state_choices([(region, region) for region in catalogs.REGIONS]),
            attribute="region",
        ),
        choice_stage(
            "deploy_source",
            "Deploy source",
            _state_choices(catalogs.DEPLOY_SOURCES),
            attribute="deploy_source",
            note=(
                "Public: no GitHub link needed; config defaults apply unless added in Render.\n"
                "Fork: required for full Render API automation and commits."
            ),
        ),
        text_stage(
            "repo_url",
            "Your fork URL",
            attribute="repo_url",
            default="https://github.com/CHANGE_ME/Aether",
            visible=lambda c: c.plan.deploy_source == "fork",
        ),
        choice_stage(
            "uptime",
            "Uptime monitoring",
            _state_choices(catalogs.UPTIME_MODES),
            attribute="uptime_mode",
        ),
        text_stage(
            "alert_email",
            "Alert email address",
            attribute="alert_email",
            visible=lambda c: c.plan.uptime_mode in {"api", "email"},
        ),
        _yes_no("local_copy", "Also set up a local copy for testing?", attribute="local_copy"),
        secret_stage(
            "render_api_key",
            "RENDER_API_KEY",
            "Render API key (or press Enter to skip)",
            hint=(
                "  A Render API key lets the deploy step create everything for you.\n"
                "  Without one the dashboard walkthrough is used.\n"
                "  https://dashboard.render.com/u/settings#api-keys"
            ),
        ),
        secret_stage(
            "database_url",
            "DATABASE_URL",
            "Postgres connection string (keep sslmode=require)",
            hint="  Paste the connection string from Neon, Render, or your own server.",
        ),
        _provider_key_stage(),
        secret_stage(
            "tavily_key",
            "TAVILY_KEY",
            "Tavily API key",
            visible=lambda c: c.has_service("web search"),
            hint="  Web search uses Tavily's free tier: https://app.tavily.com",
        ),
        secret_stage(
            "groq_key",
            "GROQ_KEY",
            "Groq API key",
            visible=lambda c: c.has_service("voice-note transcription"),
            hint="  Voice-note transcription uses Groq: https://console.groq.com/keys",
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
        ),
        secret_stage(
            "linkedin_client_secret",
            "LINKEDIN_CLIENT_SECRET",
            "LinkedIn client secret",
            visible=lambda c: c.has_app("linkedin"),
        ),
        secret_stage(
            "telegram_token",
            "TELEGRAM_TOKEN",
            "Telegram bot token",
            visible=lambda c: c.has_channel("telegram"),
            hint="  Create a bot with BotFather: https://t.me/BotFather",
        ),
        secret_stage(
            "discord_token",
            "DISCORD_TOKEN",
            "Discord bot token",
            visible=lambda c: c.has_channel("discord"),
            hint="  https://discord.com/developers/applications",
        ),
        secret_stage(
            "uptime_api_key",
            "UPTIME_API_KEY",
            "UptimeRobot main API key",
            visible=lambda c: c.plan.uptime_mode == "api",
            hint="  https://dashboard.uptimerobot.com/integrations",
        ),
    ]
    return flow
