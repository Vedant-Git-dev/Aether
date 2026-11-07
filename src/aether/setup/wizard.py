"""The wizard orchestrator: back-navigation, non-interactive mode, persistence.

Persistence happens once, at the end, atomically. Ctrl+C anywhere before that
leaves nothing half-written.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..config_validation import is_missing, is_placeholder
from . import artifacts, catalogs
from .plan import (
    SECRET_KEYS,
    SetupPlan,
    load_answers,
    write_answers,
    write_env_answers,
    write_secrets,
)
from .prompts import UI, Cancelled, GoBack
from .stages import (
    EMBEDDING_MODEL,
    GOOGLE_APPS,
    PICKER_APPS,
    PROVIDER_KEYS,
    SetupContext,
    Stage,
    build_flow,
)
from .validation import (
    database_error,
    discord_token_error,
    embedding_key_error,
    model_key_error,
    render_api_key_error,
    render_service_name_error,
    telegram_token_error,
)


@dataclass
class SetupResult:
    plan: SetupPlan
    secrets: dict[str, str] = field(default_factory=dict)
    answers_path: Path | None = None
    env_answers_path: Path | None = None
    secrets_path: Path | None = None
    config_path: Path | None = None
    render_path: Path | None = None


def environment_secrets(environ: dict[str, str] | None = None) -> dict[str, str]:
    environ = environ if environ is not None else os.environ
    return {key: environ[key] for key in SECRET_KEYS if environ.get(key)}


def run_flow(
    ctx: SetupContext, *, interactive: bool, stages: list[Stage] | None = None
) -> SetupPlan:
    """Walk the stages. Interactive prompts; non-interactive fills defaults."""
    flow = stages if stages is not None else build_flow(ctx)
    if not interactive:
        for stage in flow:
            if stage.visible(ctx):
                stage.autofill(ctx)
        catalogs.apply_dependencies(ctx.plan)
        return ctx.plan

    index = 0
    try:
        while index < len(flow):
            stage = flow[index]
            if not stage.visible(ctx):
                index += 1
                continue
            try:
                stage.run(ctx)
                index += 1
            except GoBack:
                stage.reset(ctx)
                index -= 1
                while index >= 0 and not flow[index].visible(ctx):
                    flow[index].reset(ctx)
                    index -= 1
                if index < 0:
                    raise Cancelled("back from the first question") from None
    except KeyboardInterrupt as exc:
        raise Cancelled("interrupted") from exc
    catalogs.apply_dependencies(ctx.plan)
    return ctx.plan


def required_errors(ctx: SetupContext) -> list[str]:
    """Every missing or invalid required answer, in one list.

    Non-interactive installs run this before anything is written, so a
    script fails with every problem at once instead of discovering one at
    a time (or, worse, exiting 0 with a feature that would do nothing).
    """
    problems: list[str] = []

    def check(field: str, value: object, validator=None) -> None:
        text = str(value or "").strip()
        if is_missing(text):
            problems.append(f"{field} is required")
            return
        if is_placeholder(text):
            problems.append(f"{field} is a placeholder — enter the real value")
            return
        if validator is not None:
            error = validator(text)
            if error:
                problems.append(f"{field}: {error}")

    # The core secrets are generated, so this catches a placeholder handed
    # in through the environment as well as a generation that failed.
    check("AETHER_ENCRYPTION_KEY", ctx.secret("AETHER_ENCRYPTION_KEY"))
    check("AETHER_TOKEN", ctx.secret("AETHER_TOKEN"))

    check("DATABASE_URL", ctx.secret("DATABASE_URL"), database_error)

    model = (ctx.plan.llm_model or "").strip()
    if is_missing(model) or is_placeholder(model):
        problems.append("LLM_MODEL is required")

    if ctx.plan.llm_provider != "ollama":
        entry = PROVIDER_KEYS.get(ctx.plan.llm_provider)
        if entry is not None:
            check(
                entry[0],
                ctx.secret(entry[0]),
                lambda value: model_key_error(ctx.plan.llm_provider, value),
            )

    if ctx.plan.semantic_memory:
        check(
            "GEMINI_KEY",
            ctx.secret("GEMINI_KEY"),
            lambda value: embedding_key_error(value, EMBEDDING_MODEL),
        )

    if not ctx.plan.channels:
        problems.append("choose at least one chat channel")
    if ctx.has_channel("telegram"):
        check("TELEGRAM_TOKEN", ctx.secret("TELEGRAM_TOKEN"), telegram_token_error)
    if ctx.has_channel("discord"):
        check("DISCORD_TOKEN", ctx.secret("DISCORD_TOKEN"), discord_token_error)

    if ctx.has_service("web search"):
        check("TAVILY_KEY", ctx.secret("TAVILY_KEY"))
    if ctx.has_service("voice-note transcription"):
        check("GROQ_KEY", ctx.secret("GROQ_KEY"))
    if ctx.has_service("image generation") and ctx.plan.image_provider == "gemini":
        check("GEMINI_KEY", ctx.secret("GEMINI_KEY"))

    if ctx.has_service("background jobs"):
        provider = ctx.plan.workbench_provider
        if provider == "modal":
            check("MODAL_TOKEN_ID", ctx.secret("MODAL_TOKEN_ID"))
            check("MODAL_TOKEN_SECRET", ctx.secret("MODAL_TOKEN_SECRET"))
        elif provider == "vercel":
            for key in ("VERCEL_TOKEN", "VERCEL_TEAM_ID", "VERCEL_PROJECT_ID"):
                check(key, ctx.secret(key))
        elif provider == "github_actions":
            check("GITHUB_ACTIONS_TOKEN", ctx.secret("GITHUB_ACTIONS_TOKEN"))

    if any(ctx.has_app(key) for key in GOOGLE_APPS):
        check("GOOGLE_CLIENT_ID", ctx.secret("GOOGLE_CLIENT_ID"))
        check("GOOGLE_CLIENT_SECRET", ctx.secret("GOOGLE_CLIENT_SECRET"))
    if any(ctx.has_app(key) for key in PICKER_APPS):
        check("GOOGLE_PICKER_KEY", ctx.secret("GOOGLE_PICKER_KEY"))
        check("GOOGLE_PICKER_APP_ID", ctx.secret("GOOGLE_PICKER_APP_ID"))
    if ctx.has_app("linkedin"):
        check("LINKEDIN_CLIENT_ID", ctx.secret("LINKEDIN_CLIENT_ID"))
        check("LINKEDIN_CLIENT_SECRET", ctx.secret("LINKEDIN_CLIENT_SECRET"))

    if ctx.plan.setup_target == "render":
        check("RENDER_API_KEY", ctx.secret("RENDER_API_KEY"), render_api_key_error)
        check(
            "SERVICE_NAME",
            ctx.plan.service_name,
            lambda value: render_service_name_error(value, ctx.secret("RENDER_API_KEY")),
        )
        if ctx.plan.deploy_source == "fork":
            url = (ctx.plan.repo_url or "").strip()
            if is_missing(url) or "CHANGE_ME" in url or not url.startswith("https://github.com/"):
                problems.append(
                    "REPO_URL must be your fork's https://github.com/<owner>/<repo> URL"
                )
        if ctx.plan.uptime_mode == "email":
            email = (ctx.plan.alert_email or "").strip()
            if is_missing(email) or "@" not in email:
                problems.append("ALERT_EMAIL is required when uptime monitoring is on")

    return problems


def collect(
    *,
    ui: UI,
    root: Path | None = None,
    plan: SetupPlan | None = None,
    secrets: dict[str, str] | None = None,
    interactive: bool = True,
    locked_secrets: set[str] | None = None,
    stages: list[Stage] | None = None,
) -> SetupContext:
    ctx = SetupContext(
        plan=plan or catalogs.plan_defaults(root),
        secrets=dict(secrets or {}),
        ui=ui,
        root=root,
        locked_secrets=set(locked_secrets or ()),
    )
    run_flow(ctx, interactive=interactive, stages=stages)
    # The generated core secrets exist before validation so a scripted
    # install validates exactly what it will write. persist() calls this
    # again; it never overwrites an existing value.
    artifacts.ensure_core_secrets(ctx.secrets)
    # One final set of checks for both modes: interactive already re-asked
    # until each answer was valid, and this is the gate that keeps a
    # missed or locked placeholder out of the written files.
    ctx.problems = required_errors(ctx)
    return ctx


def persist(ctx: SetupContext, state_dir: str | Path) -> SetupResult:
    """Write answers.toml, the owner-only secrets, config and blueprint."""
    directory = Path(state_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    artifacts.ensure_core_secrets(ctx.secrets)
    result = SetupResult(plan=ctx.plan, secrets=dict(ctx.secrets))
    result.answers_path = write_answers(directory / "answers.toml", ctx.plan)
    result.env_answers_path = write_env_answers(directory / "answers.env", ctx.plan)
    result.secrets_path = write_secrets(directory / "secrets.env", ctx.secrets)
    result.config_path = _write_text(
        directory / "config.generated.yaml", artifacts.build_config(ctx.plan)
    )
    result.render_path = _write_text(directory / "render.yaml", artifacts.render_yaml(ctx.plan))
    return result


def _write_text(path: Path, text: str) -> Path:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path


def preload_state(state_dir: str | Path) -> tuple[SetupPlan | None, dict[str, str]]:
    """Read answers/secrets left by an earlier run, if any."""
    directory = Path(state_dir).expanduser()
    plan: SetupPlan | None = None
    secrets: dict[str, str] = {}
    answers = directory / "answers.toml"
    if answers.is_file():
        plan, _ = load_answers(answers)
    legacy = directory / "state.env"
    if plan is None and legacy.is_file():
        plan, _ = load_answers(legacy)
    secrets_file = directory / "secrets.env"
    if secrets_file.is_file():
        _, secrets = load_answers(secrets_file)
    return plan, secrets
