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
from .artifacts import mask
from .capabilities import color_enabled, glyphs, stdout_is_tty
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
        help="Accept every default and never prompt. For scripted installs.",
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
    console.print(f"  database:    {escape(plan.database_mode)}")
    console.print(f"  model:       {escape(plan.llm_provider)} / {escape(plan.llm_model)}")
    console.print(f"  services:    {escape(', '.join(plan.services) or '(none)')}")
    console.print(f"  direct apps: {escape(', '.join(plan.apps) or '(none)')}")
    console.print(f"  channels:    {escape(', '.join(plan.channels) or '(none)')}")
    console.print(f"  plan:        {escape(plan.plan)}")
    console.print(f"  service:     {escape(plan.service_name)} ({escape(plan.region)})")
    console.print(
        f"  deploy:      {escape(plan.deploy_source)} ({escape(plan.repo_url or 'upstream')})"
    )
    console.print(f"  uptime:      {escape(plan.uptime_mode)}")
    console.print(f"  local copy:  {'yes' if plan.local_copy else 'no'}")


def _print_summary(console: Console, result: SetupResult) -> None:
    console.print()
    console.rule("Done")
    if result.answers_path:
        console.print(f"  answers:  {escape(str(result.answers_path))}")
    if result.secrets_path:
        console.print(f"  secrets:  {escape(str(result.secrets_path))}  (owner-only; back this up)")
    if result.config_path:
        console.print(f"  config:   {escape(str(result.config_path))}")
    if result.render_path:
        console.print(f"  blueprint: {escape(str(result.render_path))}")
    token = result.secrets.get("AETHER_TOKEN", "")
    if token:
        console.print(f"  AETHER_TOKEN: {mask(token)}")
    console.print()
    console.print("  Next: deploy to Render. With a Render API key and a fork, run")
    if result.env_answers_path:
        console.print(
            f"  scripts/deploy.sh --answers {escape(str(result.env_answers_path))}  (Linux)"
        )
        console.print("  or scripts\\deploy.bat --answers <same file>  (Windows).")
    console.print("  Otherwise create a Blueprint and paste the sync:false secrets.")
    console.print("  Back up AETHER_ENCRYPTION_KEY: losing it makes every stored")
    console.print("  message, memory and token unrecoverable.")


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

    _print_plan(console, ctx.plan)

    if args.dry_run:
        console.print()
        console.print("Dry run: nothing was written.")
        return 0

    result = persist(ctx, args.state_dir)
    _print_summary(console, result)
    return 0


def run() -> None:
    """Console-script wrapper."""
    raise SystemExit(main())
