from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "aether"


def python_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_no_source_module_imports_test_code() -> None:
    offenders: list[str] = []
    for path in python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                head = name.split(".")[0]
                if head in {"tests", "fakes", "software_vault", "reference_wire", "pytest"}:
                    offenders.append(f"{path.relative_to(ROOT)} imports {name}")
    assert offenders == []


def test_no_test_double_or_fixtures_are_packaged() -> None:
    assert not list(SRC.rglob("software_vault.py"))
    assert not list(SRC.rglob("reference_wire.py"))
    assert not list(SRC.rglob("vectors"))
    assert not list(SRC.rglob("*.json")) or all(
        path.name == "policy.v1.json" for path in SRC.rglob("*.json")
    )


def test_no_software_vault_switch_in_source() -> None:
    needles = ("AETHER_VAULT_FAKE", "AETHER_VAULT_SOFTWARE", "AETHER_TEST_VAULT", "SoftwareVault")
    offenders = [
        f"{path.relative_to(ROOT)}: {needle}"
        for path in python_files()
        for needle in needles
        if needle in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_vault_package_is_the_only_wire_definition() -> None:
    # The schema must not grow a second copy of the constants in tests or scripts.
    duplicates = [
        path.relative_to(ROOT)
        for path in ROOT.rglob("schema.py")
        if path.is_file() and "src/aether/vault" not in path.as_posix() and ".git" not in path.parts
    ]
    assert duplicates == []
