"""Argument parsing and the wizard entry point."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from rich.console import Console
from rich.markup import escape

from . import brand, catalogs
from .capabilities import color_enabled, glyphs, stdout_is_tty
from .deploy import deploy_engine, deploy_failure
from .panel import build_panel
from .plan import SetupPlan, load_answers
from .prompts import UI, Cancelled
from .theme import build_console
from .wizard import (
    SetupResult,
    collect,
    environment_secrets,
    persist,
    preload_state,
)

_PROG = "aether-setup"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description="Interactive setup for Aether. Arrow keys move, Space toggles, Enter continues.",
    )
    parser.add_argument(
        "--answers",
        metavar="FILE",
        help="Read answers from FILE (answers.toml or the legacy KEY=value file). "
        "Implies non-interactive.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Accept every default that has one and never prompt. Required values "
        "still have to come from the environment or an answers file.",
    )
    parser.add_argument(
        "--no-animation",
        action="store_true",
        help="Skip the intro animation.",
    )
    parser.add_argument(
        "--state-dir",
        default=os.path.expanduser("~/.aether-setup"),
        help="Where answers, secrets and the generated config are written (default ~/.aether-setup).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even when this machine already has setup state.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Collect the answers and print the plan, but write nothing.",
    )
    return parser


def _print_plan(console: Console, plan: SetupPlan) -> None:
    console.print()
    console.rule("Your Aether")
    console.print(f"  run:         {escape(plan.setup_target)}")
    console.print(f"  database:    {escape(plan.database_mode)}")
    console.print(f"  model:       {escape(plan.llm_provider)} / {escape(plan.llm_model)}")
    console.print(f"  memory:      {'on' if plan.semantic_memory else 'off'}")
    console.print(f"  services:    {escape(', '.join(plan.services) or '(none)')}")
    console.print(f"  direct apps: {escape(', '.join(plan.apps) or '(none)')}")
    console.print(f"  channels:    {escape(', '.join(plan.channels) or '(none)')}")
    if plan.setup_target == "render":
        console.print(f"  plan:        {escape(plan.plan)}")
        console.print(f"  service:     {escape(plan.service_name)} ({escape(plan.region)})")
        console.print(
            f"  deploy:      {escape(plan.deploy_source)} ({escape(plan.repo_url or 'upstream')})"
        )
        console.print(f"  uptime:      {escape(plan.uptime_mode)}")


def _print_summary(console: Console, result: SetupResult) -> None:
    console.print()
    console.rule("Done")
    if result.answers_path:
        console.print(f"  answers:  {escape(str(result.answers_path))}")
    if result.secrets_path:
        console.print(f"  secrets:  {escape(str(result.secrets_path))}  (owner-only)")
    if result.config_path:
        console.print(f"  config:   {escape(str(result.config_path))}")
    if result.render_path:
        console.print(f"  blueprint: {escape(str(result.render_path))}")
    token = result.secrets.get("AETHER_TOKEN", "")
    if token:
        console.print()
        console.print(f"  AETHER_TOKEN (panel access): {escape(token)}")
        console.print(
            "  Paste it under Settings > Access. Store it securely; it is not shown again."
        )
    if result.plan.setup_target == "local":
        console.print()
        console.print("  To run locally:")
        if os.name == "nt":
            console.print(
                f'    for /f "usebackq tokens=1,* delims==" %a in '
                f'("{escape(str(result.secrets_path))}") do @set "%a=%b"'
            )
            console.print(f"    set AETHER_CONFIG={escape(str(result.config_path))}")
            console.print("    aether")
        else:
            console.print(f"    set -a; . {escape(str(result.secrets_path))}; set +a")
            console.print(f"    AETHER_CONFIG={escape(str(result.config_path))} aether")


def main(argv: Sequence[str] | None = None, *, stream: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    stream = stream if stream is not None else sys.stdout
    color = color_enabled(stream)
    console = build_console(stream, color=color)

    non_interactive = args.yes or bool(args.answers)
    if not non_interactive and not stdout_is_tty(stream):
        console.print("[aether.stop]No terminal was found and no answers were given.[/]")
        console.print("For a scripted install run:")
        console.print(f"  {_PROG} --answers answers.toml")
        console.print(f"  {_PROG} --yes")
        return 2
    interactive = not non_interactive

    root = catalogs.repo_root()
    marks = glyphs(stream)

    animate = interactive and not args.no_animation and color
    try:
        brand.render_intro(console, animate=animate, unicode=marks.unicode)
    except KeyboardInterrupt:
        console.print()
        console.print("Setup interrupted; nothing was written.")
        return 130
    if interactive and not marks.unicode:
        console.print(
            "[aether.muted]This terminal cannot show the circle marks; "
            "using (*) and ( ) instead.[/]"
        )

    secrets = environment_secrets()
    locked = set(secrets)
    plan: SetupPlan | None = None

    if args.answers:
        loaded_plan, loaded_secrets = load_answers(args.answers)
        plan = loaded_plan
        secrets.update(loaded_secrets)
        locked |= set(loaded_secrets)

    if interactive:
        existing_plan, existing_secrets = preload_state(args.state_dir)
        if existing_plan is not None and not args.force:
            console.print("Aether setup has already run on this machine.")
            console.print(f"  state: {Path(args.state_dir).expanduser()}")
            console.print("Nothing was changed. Re-run with --force to update the deployment.")
            return 0
        if existing_plan is not None:
            plan = plan or existing_plan
            secrets.update({k: v for k, v in existing_secrets.items() if k not in secrets})
            locked |= set(existing_secrets)

    ui = UI(console=console, glyphs=marks, color=color)

    try:
        ctx = collect(
            ui=ui,
            root=root,
            plan=plan,
            secrets=secrets,
            interactive=interactive,
            locked_secrets=locked,
        )
    except Cancelled:
        console.print()
        console.print("Setup cancelled; nothing was written.")
        return 1
    except KeyboardInterrupt:
        console.print()
        console.print("Setup interrupted; nothing was written.")
        return 130

    if ctx.problems:
        console.print()
        console.print("[aether.stop]Setup cannot continue; fix these values:[/]")
        for problem in ctx.problems:
            console.print(f"  - {escape(problem)}")
        console.print()
        console.print("Nothing was written.")
        return 2

    _print_plan(console, ctx.plan)

    if args.dry_run:
        console.print()
        console.print("Dry run: nothing was written.")
        return 0

    result = persist(ctx, args.state_dir)
    if ctx.plan.setup_target == "local" and root is not None:
        panel_error = build_panel(root)
        if panel_error:
            console.print()
            console.print(f"[aether.stop]{escape(panel_error)}[/]")
            return 1
    _print_summary(console, result)
    if ctx.plan.setup_target == "render":
        if result.env_answers_path is None or root is None:
            console.print("[aether.stop]The deploy engine could not be found.[/]")
            return 1
        console.print()
        console.print("Deploying to Render...")
        code, error = deploy_engine(
            root,
            result.env_answers_path,
            Path(args.state_dir).expanduser(),
            force=args.force,
        )
        if error:
            console.print(f"[aether.stop]{escape(error)}[/]")
            return 1
        if code != 0:
            console.print(f"[aether.stop]{escape(deploy_failure(code))}[/]")
            return code
    return 0


def run() -> None:
    """Console-script wrapper."""
    raise SystemExit(main())
