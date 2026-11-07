"""Build the local panel when setup targets this machine.

The Vite panel is not committed (``src/aether/web`` is gitignored), so a
source checkout has no panel until it is built. A Render deploy builds it
in the service's build command; a local install builds it here, at the end
of setup, so one run leaves a ready panel. An installed wheel already carries
the built assets and has no ``webapp/`` to build.

The build streams its own output; this module only reports the plain
result: nothing to do, or one sentence with the fix. ``AETHER_SETUP_SKIP_PANEL``
turns the build off for a caller that knows the panel is not wanted.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def needs_build(repo_root: Path) -> bool:
    """True when the built panel is absent or older than the panel sources."""
    webapp = repo_root / "webapp"
    built = repo_root / "src" / "aether" / "web" / "index.html"
    if not (webapp / "package.json").is_file():
        return False
    if not built.is_file():
        return True
    newest = built.stat().st_mtime
    sources = [webapp / "package.json", webapp / "index.html"]
    source_dir = webapp / "src"
    if source_dir.is_dir():
        sources += list(source_dir.rglob("*"))
    return any(path.is_file() and path.stat().st_mtime > newest for path in sources)


def build_panel(repo_root: Path) -> str | None:
    """Build ``webapp`` into ``src/aether/web``. Returns an error, or None."""
    if os.environ.get("AETHER_SETUP_SKIP_PANEL"):
        return None
    if not needs_build(repo_root):
        return None
    webapp = repo_root / "webapp"
    npm = shutil.which("npm")
    if npm is None:
        return (
            "Node 22+ is needed for the local panel. Install it, then run "
            "`cd webapp && npm ci && npm run build`."
        )
    try:
        if not (webapp / "node_modules").is_dir():
            install = subprocess.run([npm, "ci"], cwd=webapp)
            if install.returncode != 0:
                return (
                    "the panel dependencies did not install. Run "
                    "`cd webapp && npm ci` and fix the error it prints."
                )
        build = subprocess.run([npm, "run", "build"], cwd=webapp)
    except OSError as exc:
        return f"the panel build could not start: {exc}"
    if build.returncode != 0:
        return "the panel build failed. Run `cd webapp && npm run build` to see why."
    return None
