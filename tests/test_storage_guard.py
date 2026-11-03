"""The storage guard: registry, boot audit, sealed write path, CI lint."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from aether.memory.crypto import ENVELOPE_MAGIC, Cipher, CryptoError
from aether.memory.storage import (
    LEGACY_PLAINTEXT,
    PLAINTEXT_ALLOWLIST,
    SEALED_COLUMNS,
    SealedStore,
    StorageGuardError,
    audit_columns,
    check_write,
    classify,
    enforce_schema,
    schema_violations,
)

ROOT = Path(__file__).resolve().parents[1]


def _load_guard_script():
    path = ROOT / "scripts" / "check_storage_guard.py"
    spec = importlib.util.spec_from_file_location("check_storage_guard", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_classify_the_three_classes() -> None:
    assert classify("events", "payload_enc") == "sealed"
    assert classify("events", "source_token") == "plaintext"
    assert classify("events", "meta") == "legacy"
    assert classify("events", "no_such_column") == "unknown"
    assert classify("no_such_table", "id") == "unknown_table"


def test_every_legacy_column_is_a_real_plaintext_risk() -> None:
    # a legacy entry may not also be allowlisted or sealed
    for table, columns in LEGACY_PLAINTEXT.items():
        for column in columns:
            assert column not in PLAINTEXT_ALLOWLIST.get(table, frozenset())
            assert column not in SEALED_COLUMNS.get(table, frozenset())


def test_every_sealed_column_is_named_enc() -> None:
    for table, columns in SEALED_COLUMNS.items():
        for column in columns:
            assert column.endswith("_enc"), f"{table}.{column} is not a sealed name"


def test_check_write_refuses_sealed_and_unknown() -> None:
    with pytest.raises(StorageGuardError, match="sealed"):
        check_write("events", ["payload_enc"])
    with pytest.raises(StorageGuardError, match="legacy"):
        check_write("events", ["meta"])
    with pytest.raises(StorageGuardError, match="classify"):
        check_write("events", ["surprise"])
    check_write("events", ["source_token", "dedup_mac"])


# ---------------------------------------------------------------------------
# the pure audit
# ---------------------------------------------------------------------------


def _row(table: str, column: str, data_type: str = "text") -> dict[str, str]:
    return {"table_name": table, "column_name": column, "data_type": data_type}


def test_audit_accepts_a_clean_schema() -> None:
    rows = [
        _row("events", "id", "bigint"),
        _row("events", "source_token", "bytea"),
        _row("events", "payload_enc", "bytea"),
    ]
    assert audit_columns(rows) == []


def test_audit_reports_legacy_unknown_and_wrong_types() -> None:
    rows = [
        _row("events", "source"),
        _row("events", "surprise"),
        _row("events", "payload_enc", "text"),
        _row("mystery", "id"),
    ]
    problems = audit_columns(rows)
    assert any("events.source" in p and "legacy" in p for p in problems)
    assert any("events.surprise" in p and "not classified" in p for p in problems)
    assert any("events.payload_enc" in p and "not bytea" in p for p in problems)
    assert any("mystery" in p and "registry" in p for p in problems)


# ---------------------------------------------------------------------------
# the CI lint
# ---------------------------------------------------------------------------


def test_lint_parses_insert_and_update_writes() -> None:
    guard = _load_guard_script()
    assert guard._written_columns(
        "INSERT INTO events (id, payload_enc, meta) VALUES ($1, $2, $3)"
    ) == [("events", "id", "payload_enc", "meta")]
    assert guard._written_columns(
        "UPDATE events SET source_token = $1, payload_enc = $2 WHERE id = $3"
    ) == [("events", "source_token", "payload_enc")]


def test_lint_flags_a_new_sql_module(tmp_path: Path) -> None:
    guard = _load_guard_script()
    path = tmp_path / "src" / "aether" / "sneaky.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "async def f(pool):\n"
        '    await pool.execute("UPDATE events SET meta = $1 WHERE id = $2", 1, 2)\n',
        encoding="utf-8",
    )
    findings = guard.check_file(path, strict=False, root=tmp_path)
    assert findings and "not in SQL_MODULES" in findings[0].message


def test_lint_flags_new_plaintext_columns_and_accepts_sealed(tmp_path: Path) -> None:
    guard = _load_guard_script()
    module = tmp_path / "src" / "aether" / "memory" / "storage.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "async def f(pool):\n"
        '    await pool.execute("UPDATE events SET payload_enc = $1 WHERE id = $2", 1, 2)\n'
        '    await pool.execute("UPDATE events SET meta = $1 WHERE id = $2", 1, 2)\n',
        encoding="utf-8",
    )
    findings = guard.check_file(module, strict=False, root=tmp_path)
    assert findings == []  # legacy is tolerated during the migration window
    strict = guard.check_file(module, strict=True, root=tmp_path)
    assert any("legacy plaintext" in f.message for f in strict)


def test_lint_flags_unclassified_writes(tmp_path: Path) -> None:
    guard = _load_guard_script()
    module = tmp_path / "src" / "aether" / "memory" / "storage.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "async def f(pool):\n"
        '    await pool.execute("UPDATE events SET subject = $1 WHERE id = $2", 1, 2)\n',
        encoding="utf-8",
    )
    findings = guard.check_file(module, strict=False, root=tmp_path)
    assert findings and "not classified" in findings[0].message


def test_lint_passes_on_this_repository() -> None:
    guard = _load_guard_script()
    assert guard.check_tree(ROOT) == []


# ---------------------------------------------------------------------------
# integration: the live schema and the sealed write path
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_live_schema_still_has_the_legacy_ledger(db) -> None:
    violations = await schema_violations(db)
    assert any("events.source" in v for v in violations)
    assert any("memory_chunks.filename" in v for v in violations)


@pytest.mark.integration
async def test_enforce_refuses_and_off_skips(db) -> None:
    with pytest.raises(StorageGuardError, match="schema audit failed"):
        await enforce_schema(db, "enforce")
    assert await enforce_schema(db, "off") == []


@pytest.mark.integration
async def test_sealed_store_binds_to_the_row(db) -> None:
    cipher = Cipher(b"s" * 32)
    store = SealedStore(db, cipher)
    identity_id = await db.fetchval(
        "INSERT INTO identities (display_name) VALUES ('someone') RETURNING id"
    )
    row = await store.insert(
        "entity_notes",
        plaintext={"identity_id": identity_id},
        sealed={"payload_enc": {"kind": "general", "note": "likes short replies"}},
    )
    note_id = row["id"]
    blob = await db.fetchval("SELECT payload_enc FROM entity_notes WHERE id = $1", note_id)
    assert blob.startswith(ENVELOPE_MAGIC)
    assert store.unseal(blob, table="entity_notes", column="payload_enc", row_id=note_id) == {
        "kind": "general",
        "note": "likes short replies",
    }
    # the record-swap attack: the same blob on another row's AAD must fail
    other = await store.insert(
        "entity_notes",
        plaintext={"identity_id": identity_id},
        sealed={"payload_enc": {"note": "other"}},
    )
    with pytest.raises(CryptoError):
        store.unseal(blob, table="entity_notes", column="payload_enc", row_id=other["id"])


@pytest.mark.integration
async def test_sealed_store_refuses_a_plaintext_sealed_column(db) -> None:
    store = SealedStore(db, Cipher(b"s" * 32))
    with pytest.raises(StorageGuardError, match="sealed"):
        await store.insert("events", plaintext={"payload_enc": b"x", "source_token": b"y"})
