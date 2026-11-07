"""Hand the collected answers to the platform's deploy engine.

The wizard is the only interactive step. When the user chooses "Deploy to
Render", the engine that creates the service and the uptime monitor runs
here, non-interactively, from the answers just written — so the user never
runs a second script. A missing engine or a non-zero exit is reported
plainly; nothing is retried behind the user's back.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def deploy_engine(
    repo_root: Path, env_answers: Path, state_dir: Path, *, force: bool = False
) -> tuple[int, str]:
    """Run the platform's deploy engine. Returns (exit code, plain error)."""
    common = [
        "--answers",
        str(env_answers),
        "--state-dir",
        str(state_dir),
        "--non-interactive",
    ]
    if force:
        common.append("--force")
    if sys.platform.startswith("win"):
        script = repo_root / "scripts" / "deploy.bat"
        command = ["cmd", "/c", str(script), *common]
    else:
        script = repo_root / "scripts" / "deploy.sh"
        command = ["bash", str(script), *common]
    if not script.is_file():
        return 1, f"deploy engine not found: {script}"
    try:
        completed = subprocess.run(command)
    except OSError as exc:
        return 1, f"could not start the deploy engine: {exc}"
    return completed.returncode, ""


def deploy_failure(code: int) -> str:
    return f"Deployment stopped with exit code {code}. Nothing was changed after that point."
