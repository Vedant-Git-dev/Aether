#!/usr/bin/env bash
# Documentation gate: relative links, markdown lint, and spelling.
#
# Missing optional tools are skipped with a clear message, so this runs on a
# bare checkout as well as CI.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-python3}"

echo "== relative links =="
"$PY" scripts/check_links.py

echo "== markdown lint =="
if command -v markdownlint-cli2 >/dev/null 2>&1; then
  markdownlint-cli2 "README.md" "CONTRIBUTING.md" "SECURITY.md" "CODE_OF_CONDUCT.md" "CHANGELOG.md" "docs/**/*.md" ".github/**/*.md"
elif command -v npx >/dev/null 2>&1; then
  if npx --no-install markdownlint-cli2 --version >/dev/null 2>&1; then
    npx --no-install markdownlint-cli2 "README.md" "CONTRIBUTING.md" "SECURITY.md" "CODE_OF_CONDUCT.md" "CHANGELOG.md" "docs/**/*.md" ".github/**/*.md"
  else
    echo "skipped: markdownlint-cli2 is not installed (npm install -g markdownlint-cli2)"
  fi
else
  echo "skipped: node and markdownlint-cli2 are not installed"
fi

echo "== spelling =="
if command -v codespell >/dev/null 2>&1; then
  codespell README.md CONTRIBUTING.md SECURITY.md CODE_OF_CONDUCT.md CHANGELOG.md docs .github scripts
else
  echo "skipped: codespell is not installed (pip install codespell)"
fi
