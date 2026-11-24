"""The crypto-boundary guard: no raw sealing outside registered modules."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str, filename: str):
    path = ROOT / "scripts" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_no_raw_crypto_outside_the_registered_boundary() -> None:
    guard = _load_script("check_crypto_boundary", "check_crypto_boundary.py")
    findings = guard.scan(ROOT)
    assert findings == [], "\n".join(f"{f.path}:{f.line}: {f.message}" for f in findings)


def test_the_guard_flags_a_module_that_seals_directly(tmp_path: Path) -> None:
    src = tmp_path / "src" / "aether"
    src.mkdir(parents=True)
    (src / "leaky.py").write_text(
        "def seal(cipher, value):\n    return cipher.encrypt_text(value, aad='x')\n",
        encoding="utf-8",
    )
    guard = _load_script("check_crypto_boundary", "check_crypto_boundary.py")
    findings = guard.scan(tmp_path)
    assert [f.message.split("()")[0] for f in findings] == ["encrypt_text"]
