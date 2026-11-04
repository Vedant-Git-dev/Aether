#!/usr/bin/env bash
#
# Aether setup (Linux and macOS) - launches the interactive setup wizard.
#
# The wizard replaced the old typed prompts: arrow keys move the highlight,
# Space toggles multi-select rows, and Enter confirms. Secrets are masked.
# Pass --answers FILE or --yes for a non-interactive install.
#
# This script only bootstraps Python and hands the arguments over; all of the
# questions, the branding and the generated artifacts live in src/aether/setup.
#
# Usage:
#   ./scripts/setup.sh [--answers FILE] [--yes] [--no-animation] [--help]

set -euo pipefail

SCRIPT_PATH=$(readlink -f "$0" 2>/dev/null || echo "$0")
SCRIPT_DIR=$(cd "$(dirname "$SCRIPT_PATH")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  printf '%s\n' \
    "[stop] python3 is required for the setup wizard." \
    "       Install it with your package manager, for example:" \
    "       sudo apt-get install -y python3" >&2
  exit 1
fi

# The wizard is importable from a plain checkout, not only an installed package.
export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

# Check Python, rich and prompt_toolkit, and install what is missing.
if ! "$PYTHON" -m aether.setup.requirements --mode setup; then
  # A system Python that refuses pip (PEP 668) gets a private environment.
  VENV="${AETHER_SETUP_VENV:-$HOME/.aether-setup/venv}"
  if ! "$PYTHON" -m aether.setup.requirements --bootstrap "$VENV"; then
    exit 1
  fi
  PYTHON="$VENV/bin/python"
fi

exec "$PYTHON" -m aether.setup "$@"
