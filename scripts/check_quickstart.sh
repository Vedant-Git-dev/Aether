#!/usr/bin/env bash
# Run the documented no-database quick start against the example config.
#
# This verifies the parts that need no external service: the package imports,
# the example config validates, the panel builds, and the fixture server
# answers. It never touches a real database and never calls a model.
#
# Usage:
#   bash scripts/check_quickstart.sh                 # fresh venv install
#   bash scripts/check_quickstart.sh --skip-install  # use the current env
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKIP_INSTALL=0
for arg in "$@"; do
  case "$arg" in
    --skip-install) SKIP_INSTALL=1 ;;
    -h|--help)
      echo "usage: bash scripts/check_quickstart.sh [--skip-install]"
      exit 0
      ;;
    *)
      echo "unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

cd "$ROOT"
PY="${PYTHON:-python3}"

"$PY" - <<'PY'
import sys
if sys.version_info < (3, 12):
    raise SystemExit("Aether needs Python 3.12 or newer")
PY

CLEANUP=""
if [ "$SKIP_INSTALL" -eq 0 ]; then
  VENV="$(mktemp -d)/venv"
  CLEANUP="$VENV"
  "$PY" -m venv "$VENV"
  # shellcheck disable=SC1091
  source "$VENV/bin/activate"
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet -e "."
  PY=python
fi

cleanup() {
  if [ -n "${SERVER_PID:-}" ]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  if [ -n "$CLEANUP" ]; then
    rm -rf "$(dirname "$CLEANUP")"
  fi
}
trap cleanup EXIT

export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

echo "1/4 checking the example config parses"
"$PY" - <<'PY'
from aether.config import load_config

config = load_config("config.example.yaml")
assert config.llm.provider in {"anthropic", "openai", "gemini", "ollama", "nvidia", "groq"}
assert config.authz.approval_ttl_hours > 0
assert config.workspace.path
print(f"   config ok: provider={config.llm.provider} model={config.llm.model}")
PY

echo "2/4 building the panel if needed"
if [ ! -f src/aether/web/index.html ]; then
  if ! command -v npm >/dev/null 2>&1; then
    echo "   npm is not installed; install Node 22 or build the panel first" >&2
    exit 1
  fi
  (cd webapp && npm ci --silent && npm run build >/dev/null)
else
  echo "   panel already built"
fi

echo "3/4 starting the fixture server on 127.0.0.1:8731"
"$PY" tests/e2e/dev_server.py >/tmp/aether-quickstart-server.log 2>&1 &
SERVER_PID=$!

"$PY" - <<'PY'
import sys
import time
import urllib.error
import urllib.request

url = "http://127.0.0.1:8731/"
deadline = time.time() + 30
last = ""
while time.time() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            if response.status == 200:
                sys.exit(0)
            last = f"status {response.status}"
    except (urllib.error.URLError, OSError) as exc:
        last = str(exc)
    time.sleep(0.5)
raise SystemExit(f"fixture server did not answer: {last}")
PY

echo "4/4 checking health and the panel"
curl --silent --fail http://127.0.0.1:8731/healthz >/dev/null
curl --silent --fail http://127.0.0.1:8731/ >/dev/null
echo "   fixture server and panel answered"

echo "Quick start check passed."
