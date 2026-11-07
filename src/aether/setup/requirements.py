"""Requirement checks and best-effort installs for setup and deploy.

Stdlib only, so the launchers can run it before ``rich`` and ``prompt_toolkit``
exist. The launchers call ``python -m aether.setup.requirements --mode setup``
or ``--mode deploy``; this module reports what is missing and installs what it
safely can (Python packages with pip, system tools with the platform's package
manager). Anything it cannot install it reports with the exact command to run.
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

MIN_PYTHON = (3, 12)

# Distribution name -> import name. ``rich`` and ``prompt_toolkit`` are the
# wizard's own dependencies and are declared in pyproject.toml.
SETUP_PACKAGES: dict[str, str] = {"rich": "rich", "prompt_toolkit": "prompt_toolkit"}

# External commands the POSIX deploy engine cannot run without. `curl` posts
# to the Render API; `base64` generates AETHER_ENCRYPTION_KEY / AETHER_TOKEN
# and encodes config.yaml into AETHER_CONFIG_B64. Windows does both with
# PowerShell's [Convert]::ToBase64String, so it only needs curl.exe.
DEPLOY_COMMANDS: tuple[str, ...] = ("curl", "base64")
DEPLOY_COMMANDS_WINDOWS: tuple[str, ...] = ("curl",)

# The distro package that provides a command, when it is not the command name.
SYSTEM_PACKAGES: dict[str, str] = {"base64": "coreutils", "curl": "curl"}


def deploy_commands(platform: str | None = None) -> tuple[str, ...]:
    """The commands the deploy engine uses on this platform.

    ``base64`` is POSIX-only. On Windows the batch engine routes every
    encode and key/token generation through PowerShell, so requiring a
    ``base64`` executable there would refuse a machine that can deploy.
    """
    if (platform or sys.platform).startswith("win"):
        return DEPLOY_COMMANDS_WINDOWS
    return DEPLOY_COMMANDS


# A system tool installs through the first of these package managers found.
SYSTEM_INSTALLERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("apt-get", ("sudo", "-n", "apt-get", "install", "-y")),
    ("dnf", ("sudo", "-n", "dnf", "install", "-y")),
    ("yum", ("sudo", "-n", "yum", "install", "-y")),
    ("apk", ("sudo", "-n", "apk", "add")),
    ("pacman", ("sudo", "-n", "pacman", "-S", "--noconfirm")),
    ("zypper", ("sudo", "-n", "zypper", "install", "-y")),
    ("brew", ("brew", "install")),
)


@dataclass
class Missing:
    """What a preflight could not find, split by how it can be fixed."""

    python_too_old: bool = False
    packages: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return self.python_too_old or bool(self.packages) or bool(self.commands)


def python_ok(version_info: Sequence[int] | None = None) -> bool:
    info = tuple(version_info if version_info is not None else sys.version_info)
    return info[:2] >= MIN_PYTHON


def missing_packages(
    packages: Mapping[str, str] | None = None,
    *,
    find_spec: Callable[[str], object | None] | None = None,
) -> list[str]:
    packages = packages if packages is not None else SETUP_PACKAGES
    finder = find_spec if find_spec is not None else importlib.util.find_spec
    return [name for name, module in packages.items() if finder(module) is None]


def missing_commands(
    commands: Iterable[str],
    *,
    which: Callable[[str], str | None] | None = None,
) -> list[str]:
    finder = which if which is not None else shutil.which
    return [name for name in commands if finder(name) is None]


def check(
    mode: str,
    *,
    version_info: Sequence[int] | None = None,
    find_spec: Callable[[str], object | None] | None = None,
    which: Callable[[str], str | None] | None = None,
    platform: str | None = None,
) -> Missing:
    """Report what a setup or deploy run is missing, without changing anything."""
    result = Missing()
    if mode == "setup":
        # The wizard is written for 3.12+; the deploy engine has fallbacks and
        # does not block on the interpreter version.
        result.python_too_old = not python_ok(version_info)
        result.packages = missing_packages(find_spec=find_spec)
    else:
        result.commands = missing_commands(deploy_commands(platform), which=which)
    return result


def install_python_packages(
    packages: Iterable[str],
    *,
    python: str | None = None,
    run: Callable[..., subprocess.CompletedProcess] | None = None,
) -> bool:
    """Install pip packages, falling back to ``--user``. Returns success."""
    packages = list(packages)
    if not packages:
        return True
    python = python or sys.executable
    runner = run or subprocess.run

    def attempt(extra: list[str]) -> bool:
        proc = runner(
            [python, "-m", "pip", "install", "--quiet", *extra, *packages],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        return getattr(proc, "returncode", 1) == 0

    if attempt([]):
        return True
    # An externally-managed or read-only install often needs --user.
    return attempt(["--user"])


def install_system_command(
    command: str,
    *,
    package: str | None = None,
    which: Callable[[str], str | None] | None = None,
    run: Callable[..., subprocess.CompletedProcess] | None = None,
) -> bool:
    """Best-effort install of one system tool. Returns success."""
    finder = which if which is not None else shutil.which
    runner = run or subprocess.run
    package = package or SYSTEM_PACKAGES.get(command, command)
    for manager, prefix in SYSTEM_INSTALLERS:
        if finder(manager) is None:
            continue
        proc = runner([*prefix, package], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if getattr(proc, "returncode", 1) == 0:
            return True
    return False


def venv_python(venv_dir: str | Path, *, platform: str | None = None) -> Path:
    """The interpreter inside a virtual environment, per platform."""
    venv = Path(venv_dir)
    if (platform or sys.platform).startswith("win"):
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def bootstrap_venv(
    venv_dir: str | Path,
    packages: Iterable[str] | None = None,
    *,
    python: str | None = None,
    platform: str | None = None,
    run: Callable[..., subprocess.CompletedProcess] | None = None,
    out: Callable[[str], None] = print,
) -> Path | None:
    """Create/reuse a virtual environment and install the packages into it.

    Returns the interpreter to run, or None when the environment could not be
    prepared. Needed on systems with PEP 668's externally-managed marker, where
    a plain ``pip install`` is refused.
    """
    packages = list(packages if packages is not None else SETUP_PACKAGES)
    python = python or sys.executable
    runner = run or subprocess.run
    interpreter = venv_python(venv_dir, platform=platform)

    if not interpreter.exists():
        out(f"[ .. ] creating a virtual environment at {venv_dir}")
        proc = runner(
            [python, "-m", "venv", str(venv_dir)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if getattr(proc, "returncode", 1) != 0:
            out("[warn] could not create a virtual environment")
            return None

    out(f"[ .. ] installing {', '.join(packages)} into {venv_dir}")
    if not install_python_packages(packages, python=str(interpreter), run=runner):
        out("[warn] could not install the packages into the virtual environment")
        return None
    return interpreter


def ensure(
    mode: str,
    *,
    install: bool = True,
    version_info: Sequence[int] | None = None,
    find_spec: Callable[[str], object | None] | None = None,
    which: Callable[[str], str | None] | None = None,
    run: Callable[..., subprocess.CompletedProcess] | None = None,
    out: Callable[[str], None] = print,
) -> Missing:
    """Check requirements and install what was asked; report what is left."""
    before = check(mode, version_info=version_info, find_spec=find_spec, which=which)
    if not install or not before.any:
        return before

    if before.packages:
        out(f"[ .. ] installing Python packages: {', '.join(before.packages)}")
        if not install_python_packages(before.packages, run=run):
            out("[warn] pip install failed; try: pip install -e .")

    for command in before.commands:
        out(f"[ .. ] installing system tool: {command}")
        if not install_system_command(command, which=which, run=run):
            out(f"[warn] could not install '{command}' automatically")

    # Re-check with the same probes so the report reflects reality.
    return check(mode, version_info=version_info, find_spec=find_spec, which=which)


def _report(missing: Missing, out: Callable[[str], None]) -> None:
    if missing.python_too_old:
        out(f"[stop] Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required.")
        if sys.platform.startswith("win"):
            out("       Install it from https://www.python.org/downloads/")
        elif sys.platform == "darwin":
            out("       Install it with: brew install python@3.12")
        else:
            out("       Install it with your package manager, for example:")
            out("       sudo apt-get install -y python3")
    if missing.packages:
        out(f"[stop] missing Python packages: {', '.join(missing.packages)}")
        out("       Install them with: pip install -e .")
    if missing.commands:
        out(f"[stop] missing tools: {', '.join(missing.commands)}")
        names = " ".join(SYSTEM_PACKAGES.get(item, item) for item in missing.commands)
        out(
            "       Install them with your package manager, for example: "
            f"sudo apt-get install -y {names}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m aether.setup.requirements",
        description="Check and install Aether's setup or deploy requirements.",
    )
    parser.add_argument("--mode", choices=("setup", "deploy"), default="setup")
    parser.add_argument(
        "--no-install",
        action="store_true",
        help="Only report; never install anything.",
    )
    parser.add_argument(
        "--bootstrap",
        metavar="DIR",
        help="Create a virtual environment at DIR and install the setup "
        "packages into it. Prints [ ok ] and exits 0 on success.",
    )
    args = parser.parse_args(argv)

    out = print
    if args.bootstrap:
        interpreter = bootstrap_venv(args.bootstrap, out=out)
        if interpreter is None:
            return 1
        missing = requirements_check(interpreter)
        if missing:
            out(f"[warn] the new environment still cannot import: {', '.join(missing)}")
            return 1
        out(f"[ ok ] virtual environment ready: {interpreter}")
        return 0

    missing = ensure(args.mode, install=not args.no_install, out=out)
    if missing.any:
        _report(missing, out)
        return 1
    out("[ ok ] requirements satisfied")
    return 0


def requirements_check(interpreter: str | Path) -> list[str]:
    """Ask a (possibly virtual) interpreter which setup packages it lacks."""
    code = (
        "import importlib.util,sys;"
        "print(','.join(n for n in ('rich','prompt_toolkit') "
        "if importlib.util.find_spec(n) is None))"
    )
    try:
        proc = subprocess.run(
            [str(interpreter), "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError:
        return list(SETUP_PACKAGES)
    if proc.returncode != 0:
        return list(SETUP_PACKAGES)
    output = (proc.stdout or "").strip()
    return [name for name in output.split(",") if name]


if __name__ == "__main__":
    raise SystemExit(main())
