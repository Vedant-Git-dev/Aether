#!/usr/bin/env python3
"""The sealed-storage CI guard.

Two rules, both enforced offline against the source tree:

1. SQL lives only in the modules named in SQL_MODULES. New code writes
   through `memory/storage.py`; this list shrinks as the legacy stores are
   migrated and must end at the storage API itself (plus the migration
   runner, whose statements touch only schema_migrations).

2. Every column written by an INSERT or UPDATE is classified in the
   storage registry as plaintext-allowed or sealed. A new plaintext column
   — the common way user content leaks back into SQL — fails here.

`--strict` also fails while any legacy plaintext column remains. CI uses
the default during the migration window and switches to --strict when the
phase lands.

Run: python scripts/check_storage_guard.py [--strict] [--root PATH]
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aether.memory.allowlist import (  # noqa: E402
    LEGACY_PLAINTEXT,
    classify,
)

# Modules still allowed to carry SQL. Shrinks to memory/storage.py and
# memory/db.py by the end of the storage phase.
SQL_MODULES = frozenset(
    {
        "src/aether/agent/settings.py",
        "src/aether/agent/traces.py",
        "src/aether/api/routes.py",
        "src/aether/authz/approvals.py",
        "src/aether/authz/audit.py",
        "src/aether/chat/ws.py",
        "src/aether/config_store.py",
        "src/aether/connectors/processed.py",
        "src/aether/counters.py",
        "src/aether/media/store.py",
        "src/aether/memory/crypto.py",
        "src/aether/memory/db.py",
        "src/aether/memory/commitments.py",
        "src/aether/memory/conclusions.py",
        "src/aether/memory/control.py",
        "src/aether/memory/deliveries.py",
        "src/aether/memory/entities.py",
        "src/aether/memory/events.py",
        "src/aether/memory/facts.py",
        "src/aether/memory/focus.py",
        "src/aether/memory/inbound.py",
        "src/aether/memory/notifications.py",
        "src/aether/memory/seal_migrate.py",
        "src/aether/memory/storage.py",
        "src/aether/memory/summaries.py",
        "src/aether/memory/vectors.py",
        "src/aether/memory/watches.py",
        "src/aether/proactive/path.py",
        "src/aether/proactive/store.py",
        "src/aether/providers/rotate_tokens.py",
        "src/aether/providers/store.py",
        "src/aether/routines/store.py",
        "src/aether/scheduler/jobs.py",
        "src/aether/secret_store.py",
        "src/aether/workbench/minutes.py",
        "src/aether/workbench/transfers.py",
    }
)

_SQL_START = re.compile(
    r"^\s*(insert\s+into|update\s+[a-z_][a-z0-9_]*\s+set|delete\s+from|"
    r"select\s+.+\s+from|with\s+\w+\s+as|create\s+table|alter\s+table|truncate\s+)",
    re.IGNORECASE | re.DOTALL,
)
_INSERT = re.compile(
    r"\binsert\s+into\s+([a-z_][a-z0-9_]*)\s*\(([^)]*)\)", re.IGNORECASE | re.DOTALL
)
_INSERT_NO_COLUMNS = re.compile(r"\binsert\s+into\s+([a-z_][a-z0-9_]*)\b(?!\s*\()", re.IGNORECASE)
_UPDATE = re.compile(
    r"\bupdate\s+([a-z_][a-z0-9_]*)\s+set\s+(.*?)(?:\bwhere\b|\breturning\b|$)",
    re.IGNORECASE | re.DOTALL,
)
_ASSIGNMENT = re.compile(r"^\s*([a-z_][a-z0-9_]*)\s*=", re.IGNORECASE)
_COLUMN = re.compile(r"^[a-z_][a-z0-9_]*$")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    message: str


def _strings(tree: ast.AST) -> list[tuple[int, str]]:
    """Every string constant that opens like a SQL statement."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        text = node.value
        if _SQL_START.match(text):
            found.append((getattr(node, "lineno", 0), text))
    return found


def _written_columns(sql: str) -> list[tuple[str, ...]]:
    """(table, columns...) for INSERT/UPDATE statements; empty when the
    statement is a read, a schema change, or has no column list."""
    out: list[tuple[str, ...]] = []
    for match in _INSERT.finditer(sql):
        table = match.group(1).lower()
        columns = tuple(part.strip().lower() for part in match.group(2).split(",") if part.strip())
        out.append((table, *columns))
    for match in _UPDATE.finditer(sql):
        table = match.group(1).lower()
        columns: list[str] = []
        for chunk in match.group(2).split(","):
            assignment = _ASSIGNMENT.match(chunk)
            if assignment:
                columns.append(assignment.group(1).lower())
        if columns:
            out.append((table, *columns))
    return out


def check_file(path: Path, *, strict: bool, root: Path = ROOT) -> list[Finding]:
    rel = path.relative_to(root).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:  # a broken file is a different job's failure
        return [Finding(rel, exc.lineno or 0, f"could not parse: {exc.msg}")]
    strings = _strings(tree)
    if not strings:
        return []
    findings: list[Finding] = []
    if rel not in SQL_MODULES:
        line, _ = strings[0]
        findings.append(
            Finding(rel, line, "contains SQL but is not in SQL_MODULES — use memory/storage.py")
        )
        return findings
    for line, sql in strings:
        if _INSERT_NO_COLUMNS.search(sql) and not _INSERT.search(sql):
            findings.append(Finding(rel, line, "INSERT without a column list is not auditable"))
        for entry in _written_columns(sql):
            table, columns = entry[0], entry[1:]
            for column in columns:
                if not _COLUMN.match(column):
                    continue
                kind = classify(table, column)
                if kind == "plaintext" or kind == "sealed":
                    continue
                if kind == "legacy" and not strict:
                    continue
                if kind == "legacy":
                    findings.append(
                        Finding(
                            rel,
                            line,
                            f"{table}.{column} is legacy plaintext — seal it before --strict",
                        )
                    )
                else:
                    findings.append(
                        Finding(rel, line, f"{table}.{column} is not classified ({kind})")
                    )
    return findings


def check_tree(root: Path = ROOT, *, strict: bool = False) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted((root / "src" / "aether").rglob("*.py")):
        findings.extend(check_file(path, strict=strict, root=root))
    if strict:
        for table, columns in LEGACY_PLAINTEXT.items():
            for column in sorted(columns):
                findings.append(
                    Finding(
                        "src/aether/memory/storage.py",
                        0,
                        f"legacy ledger still lists {table}.{column}",
                    )
                )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="also fail on legacy plaintext")
    parser.add_argument("--root", default=str(ROOT), help="repository root")
    args = parser.parse_args(argv)
    findings = check_tree(Path(args.root), strict=args.strict)
    if not findings:
        print("storage guard: every SQL write is allowlisted or sealed")
        return 0
    for finding in findings:
        where = f"{finding.path}:{finding.line}" if finding.line else finding.path
        print(f"{where}: {finding.message}")
    print(f"\nstorage guard: {len(findings)} finding(s)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
