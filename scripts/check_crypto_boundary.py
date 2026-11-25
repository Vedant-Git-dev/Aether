#!/usr/bin/env python3
"""The crypto-boundary CI guard.

Sealing and opening content must go through a registered boundary: the
`rows` seam (`CipherRows`/`ScopedRows` and the per-family row classes) on
the serving path, or a `KeyScope` on the scoped path. A store that calls
`cipher.encrypt_*` / `decrypt_*` / `blind_token` / `dedup_mac` directly is
a bug the type system cannot catch, because it bypasses the row seam and
the location registry.

This walks `src/aether` and fails when a module outside CRYPTO_BOUNDARY
calls one of those primitives. Tests and dev tooling are out of scope.

Run: python scripts/check_crypto_boundary.py [--root PATH]
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

# Modules allowed to touch the raw sealing primitives: the crypto module
# itself, the row seams, legacy migration readers, the provider token
# family, and the vault primitives.
CRYPTO_BOUNDARY = frozenset(
    {
        "src/aether/memory/crypto.py",
        "src/aether/memory/rows.py",
        "src/aether/memory/storage.py",
        "src/aether/memory/event_rows.py",
        "src/aether/memory/body_rows.py",
        "src/aether/memory/entity_rows.py",
        "src/aether/memory/seal_migrate.py",
        "src/aether/providers/crypto.py",
        "src/aether/providers/rotate_tokens.py",
        "src/aether/providers/store.py",
        "src/aether/alerts/migrate.py",
        "src/aether/workspace/store.py",
        "src/aether/vault/native.py",
        "src/aether/vault/scope.py",
        "src/aether/vault/store.py",
        "src/aether/vault/operator_migrate.py",
    }
)

# The sealing/opening primitives. Key generation (`generate_key_b64`) and
# cipher construction are not included: only the act of sealing bytes.
PRIMITIVES = frozenset(
    {
        "encrypt_bytes",
        "encrypt_text",
        "encrypt_json",
        "decrypt_bytes",
        "decrypt_text",
        "decrypt_json",
        "blind_token",
        "dedup_mac",
    }
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    message: str


def _calls(tree: ast.AST) -> list[tuple[int, str]]:
    """Every direct call to a sealing primitive, as (line, name)."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in PRIMITIVES:
            found.append((getattr(node, "lineno", 0), func.attr))
    return found


def scan(root: Path) -> list[Finding]:
    src = root / "src" / "aether"
    findings: list[Finding] = []
    if not src.is_dir():
        return findings
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel in CRYPTO_BOUNDARY:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            findings.append(Finding(rel, exc.lineno or 0, f"could not parse: {exc.msg}"))
            continue
        for line, name in _calls(tree):
            findings.append(
                Finding(
                    rel,
                    line,
                    f"{name}() is a raw sealing primitive — seal through the rows seam "
                    "or a KeyScope, or register the module in CRYPTO_BOUNDARY",
                )
            )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reject raw crypto outside the boundary")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args(argv)

    findings = scan(Path(args.root))
    if not findings:
        print("crypto boundary: ok")
        return 0
    for finding in findings:
        print(f"{finding.path}:{finding.line}: {finding.message}")
    print(f"\n{len(findings)} violation(s) of the crypto boundary")
    return 1


if __name__ == "__main__":
    sys.exit(main())
