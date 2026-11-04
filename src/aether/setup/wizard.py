"""The wizard orchestrator: back-navigation, non-interactive mode, persistence.

Persistence happens once, at the end, atomically. Ctrl+C anywhere before that
leaves nothing half-written.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

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
from .stages import SetupContext, Stage, build_flow


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
