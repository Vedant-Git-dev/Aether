"""Offline copying of verified legacy records into a separate AE2 database."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import asyncpg
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ..authz.audit import (
    GENESIS_HASH,
    compute_sealed_entry_hash,
    legacy_canonical,
    matches_checkpoint,
    verify_entries,
)
from ..connectors.processed import update_token_input
from ..memory.allowlist import SEALED_COLUMNS, STORAGE_CATEGORIES, classify
from ..memory.crypto import Cipher, CryptoError
from ..memory.entities import normalize_handle
from ..memory.events import canonical_payload
from ..memory.storage import ScopedStore
from ..providers.crypto import TokenKeyError, TokenKeyring
from .scope import KeyScope
from .store import VaultStore


class MaintenanceRefused(RuntimeError):
    """The job cannot proceed safely: it needs an owner decision or a key
    the operator does not hold."""


@dataclass(frozen=True)
class LegacyKeys:
    """The old active and previous content keys, plus the token keyring."""

    active: Cipher
    previous: Cipher | None = None
    keyring: TokenKeyring | None = None


def _decode(raw: bytes, codec: str) -> Any:
    if codec == "json":
        return json.loads(raw.decode("utf-8"))
    if codec == "text":
        return raw.decode("utf-8")
    return raw


def open_legacy(
    keys: LegacyKeys,
    blob: bytes,
    aad: str,
    *,
    family: str = "content",
    key_id: str | None = None,
) -> bytes:
    """Open one legacy blob with the exact AAD it was sealed under. Tries
    the active key, then the previous one; a miss is a missing historical
    key, which stops the job."""
    if family == "token":
        if keys.keyring is None or key_id is None:
            raise MaintenanceRefused("a token-key field needs the token keyring and its key id")
        try:
            return keys.keyring.decrypt_text(bytes(blob), aad, key_id).encode("utf-8")
        except (TokenKeyError, CryptoError):
            raise MaintenanceRefused("a legacy token field could not be opened") from None
    for cipher in (keys.active, keys.previous):
        if cipher is None:
            continue
        try:
            return cipher.decrypt_bytes(blob, aad)
        except CryptoError:
            continue
        except Exception:  # noqa: BLE001 - any open failure is a refusal
            raise MaintenanceRefused("a legacy content field could not be opened") from None
    raise MaintenanceRefused("no held key opens a legacy content field")


@dataclass(frozen=True)
class SealedColumn:
    column: str
    codec: str  # "json" | "text" | "bytes"
    aad: Callable[[asyncpg.Record], str]
    family: str = "content"


@dataclass(frozen=True)
class TokenColumn:
    column: str
    domain: str
    legacy_purpose: str | None
    # the token input, derived from the row's already-opened plaintexts
    derive: Callable[[dict[str, Any], asyncpg.Record], str | bytes]
    # a content dedup tag (scope.dedup) rather than an equality lookup
    dedup: bool = False


@dataclass(frozen=True)
class TableSpec:
    table: str
    pk: str
    cat: str
    columns: tuple[SealedColumn, ...]
    tokens: tuple[TokenColumn, ...] = ()
    key_id_column: str | None = None
    ver: int = 1
    # a companion table whose rows are tombstone inputs that cannot be
    # reconstructed — its presence can stop the job
    tombstone: str | None = None


def _plain_aad(table: str, column: str, key: str = "id") -> Callable[[asyncpg.Record], str]:
    return lambda row: f"{table}:{column}:{row[key]}"


def _composite_aad(table: str, column: str, key: str) -> Callable[[asyncpg.Record], str]:
    return lambda row: f"{table}:{column}:{row[key]}"


def _row_spec(
    table: str,
    fields: tuple[tuple[str, str], ...],
    *,
    pk: str = "id",
    legacy_table: str | None = None,
    tokens: tuple[TokenColumn, ...] = (),
) -> TableSpec:
    return TableSpec(
        table,
        pk,
        STORAGE_CATEGORIES[table],
        tuple(
            SealedColumn(column, codec, _plain_aad(legacy_table or table, column, pk))
            for column, codec in fields
        ),
        tokens,
    )


def _event_dedup(meta: dict[str, Any], payload: dict[str, Any]) -> bytes:
    if (key := meta.get("dedup_key")) is not None:
        return b"dedup:" + key.encode("utf-8")
    return canonical_payload(meta["kind"], payload)


def _fact_input(plains: dict[str, Any], row: asyncpg.Record) -> str:
    body = plains["body_enc"]
    subject = " ".join(str(body.get("subject") or "").split()).lower()
    return f"{row['subject_identity_id'] or 0}\x1f{subject}\x1f{body['key']}"


# The migration registry. Every family whose row can be re-sealed from data
# already on the row has a spec; a table without one is refused, never
# guessed at.
SPECS: dict[str, TableSpec] = {
    "events": TableSpec(
        table="events",
        pk="id",
        cat="memory",
        columns=(
            SealedColumn("payload_enc", "json", _plain_aad("events", "payload_enc")),
            SealedColumn("meta_enc", "json", _plain_aad("events", "meta_enc")),
        ),
        tokens=(
            TokenColumn(
                "source_token",
                "event.source",
                "source",
                lambda plains, _: plains["meta_enc"]["source"],
            ),
            TokenColumn(
                "dedup_mac",
                "event.dedup",
                "dedup",
                lambda plains, _: _event_dedup(plains["meta_enc"], plains["payload_enc"]),
                dedup=True,
            ),
        ),
        tombstone="forgotten_events",
    ),
    "workspace_files": TableSpec(
        table="workspace_files",
        pk="id",
        cat="workspace",
        columns=(
            SealedColumn("name_enc", "text", _plain_aad("workspace_files", "name_enc")),
            SealedColumn("content_enc", "text", _plain_aad("workspace_files", "content_enc")),
        ),
        tokens=(
            TokenColumn(
                "name_token",
                "workspace.name",
                "workspace",
                lambda plains, _: plains["name_enc"],
            ),
        ),
    ),
    "media_blobs": TableSpec(
        table="media_blobs",
        pk="id",
        cat="media",
        columns=(
            SealedColumn("bytes_enc", "bytes", lambda row: f"media:bytes_enc:{row['id']}"),
            SealedColumn("meta_enc", "json", _plain_aad("media_blobs", "meta_enc")),
        ),
        tokens=(
            TokenColumn(
                "dedup_mac",
                "media.content",
                "dedup",
                lambda plains, _: plains["bytes_enc"],
                dedup=True,
            ),
            TokenColumn(
                "origin_token",
                "media.origin",
                "source",
                lambda plains, _: plains["meta_enc"]["origin"],
            ),
        ),
        tombstone="forgotten_events",
    ),
    "deliveries": TableSpec(
        table="deliveries",
        pk="id",
        cat="operations",
        columns=(
            SealedColumn("body_enc", "json", _plain_aad("deliveries", "body_enc")),
            SealedColumn("message_id_enc", "text", _plain_aad("deliveries", "message_id_enc")),
            SealedColumn("error_enc", "text", _plain_aad("deliveries", "error_enc")),
        ),
        tokens=(
            TokenColumn(
                "surface_token",
                "delivery.surface",
                "delivery",
                lambda plains, _: plains["body_enc"]["surface"],
            ),
        ),
    ),
    "provider_connections": TableSpec(
        table="provider_connections",
        pk="provider",
        cat="credentials",
        columns=(
            SealedColumn(
                "access_enc",
                "text",
                lambda row: f"provider_connections:access_enc:{row['provider']}",
                "token",
            ),
            SealedColumn(
                "refresh_enc",
                "text",
                lambda row: f"provider_connections:refresh_enc:{row['provider']}",
                "token",
            ),
            SealedColumn(
                "account_label_enc",
                "text",
                _composite_aad("provider_connections", "account_label_enc", "provider"),
            ),
            SealedColumn(
                "account_id_enc",
                "text",
                _composite_aad("provider_connections", "account_id_enc", "provider"),
            ),
            SealedColumn(
                "last_error_enc",
                "text",
                _composite_aad("provider_connections", "last_error_enc", "provider"),
            ),
        ),
        key_id_column="key_id",
    ),
    "provider_oauth_states": TableSpec(
        table="provider_oauth_states",
        pk="state_hash",
        cat="credentials",
        columns=(
            SealedColumn(
                "code_verifier_enc",
                "text",
                lambda row: f"provider_oauth_states:code_verifier_enc:{row['state_hash']}",
                "token",
            ),
        ),
        key_id_column="key_id",
    ),
    "identities": _row_spec("identities", (("profile_enc", "json"),)),
    "identity_handles": _row_spec(
        "identity_handles",
        (("handle_enc", "json"),),
        tokens=(
            TokenColumn(
                "platform_token",
                "identity.platform",
                "platform",
                lambda p, _: p["handle_enc"]["platform"],
            ),
            TokenColumn(
                "normalized_token",
                "identity.handle",
                "handle",
                lambda p, _: normalize_handle(
                    p["handle_enc"]["platform"], p["handle_enc"]["handle"]
                ),
            ),
        ),
    ),
    "entity_notes": _row_spec("entity_notes", (("payload_enc", "json"),)),
    "chat_messages": _row_spec(
        "chat_messages",
        (("payload_enc", "json"),),
        tokens=(
            TokenColumn(
                "surface_token", "chat.surface", "source", lambda p, _: p["payload_enc"]["surface"]
            ),
        ),
    ),
    "memory_chunks": TableSpec(
        "memory_chunks",
        "id",
        "memory",
        (
            SealedColumn("text_enc", "text", _plain_aad("chunks", "text_enc")),
            SealedColumn("embedding_enc", "bytes", _plain_aad("chunks", "embedding_enc")),
            SealedColumn("meta_enc", "json", _plain_aad("memory_chunks", "meta_enc")),
        ),
        (
            TokenColumn(
                "embed_model_token",
                "embed.model",
                "embed_model",
                lambda p, _: p["meta_enc"]["embed_model"],
            ),
        ),
    ),
    "facts": _row_spec(
        "facts",
        (("body_enc", "json"),),
        tokens=(TokenColumn("key_token", "fact.subject", "fact_key", _fact_input),),
    ),
    "summaries": _row_spec(
        "summaries",
        (("body_enc", "json"),),
        tokens=(
            TokenColumn(
                "period_token",
                "summary.period",
                "summary_period",
                lambda p, row: f"{row['kind']}\x1f{p['body_enc']['period']}",
            ),
        ),
    ),
    "commitments": _row_spec("commitments", (("body_enc", "json"),)),
    "held_notes": _row_spec("held_notes", (("body_enc", "json"),)),
    "pending_approvals": _row_spec(
        "pending_approvals",
        (("params_enc", "json"), ("rules_matched_enc", "text"), ("note_enc", "text")),
    ),
    "scheduled_actions": _row_spec(
        "scheduled_actions", (("payload_enc", "json"), ("meta_enc", "json"))
    ),
    "decision_traces": _row_spec("decision_traces", (("label_enc", "text"), ("trace_enc", "json"))),
    "agent_settings": _row_spec("agent_settings", (("personality_enc", "json"),)),
    "config_overrides": _row_spec("config_overrides", (("value_enc", "json"),), pk="path"),
    "connector_cursors": _row_spec("connector_cursors", (("last_seen_enc", "text"),), pk="source"),
    "routines_legacy": _row_spec(
        "routines_legacy",
        (("label_enc", "text"), ("trigger_enc", "json"), ("action_enc", "json")),
        legacy_table="routines",
    ),
    "app_secrets": TableSpec(
        "app_secrets",
        "name",
        "credentials",
        (SealedColumn("value_enc", "text", _plain_aad("app_secrets", "value", "name")),),
    ),
    "oauth_tokens": _row_spec(
        "oauth_tokens",
        (("access_enc", "text"), ("refresh_enc", "text")),
        pk="provider",
    ),
    "processed_updates": TableSpec(
        "processed_updates",
        "id",
        "operations",
        (),
        (
            TokenColumn(
                "dedup_token",
                "processed.update",
                None,
                lambda _, row: update_token_input(row["source"], row["update_id"]),
            ),
        ),
    ),
    "workbench_transfers": TableSpec(
        "workbench_transfers",
        "token_hash",
        "operations",
        (
            SealedColumn(
                "payload_enc",
                "json",
                lambda row: f"workbench:transfer:{bytes(row['token_hash']).hex()}",
            ),
        ),
    ),
}

_AUDIT_SPEC = TableSpec("audit_log", "seq", "operations", ())


@dataclass
class TableReport:
    table: str
    rows: int = 0
    migrated: int = 0
    resumed: int = 0


@dataclass
class MigrationReport:
    tables: list[TableReport] = field(default_factory=list)

    def for_table(self, table: str) -> TableReport:
        for report in self.tables:
            if report.table == table:
                return report
        report = TableReport(table=table)
        self.tables.append(report)
        return report


def stable_id(table: str, pk: Any) -> bytes:
    """A 32-byte opaque, deterministic reference for one source row."""
    return hashlib.sha256(f"{table}:{pk}".encode()).digest()


async def _table_inventory(conn: asyncpg.Connection) -> set[str]:
    return {
        row["tablename"]
        for row in await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
        )
    }


async def _columns(conn: asyncpg.Connection, table: str) -> tuple[str, ...]:
    return tuple(
        row["column_name"]
        for row in await conn.fetch(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = $1"
            " ORDER BY ordinal_position",
            table,
        )
    )


def _check_scope(scope: KeyScope, specs: list[TableSpec]) -> None:
    if scope.operation != "maintenance":
        raise MaintenanceRefused("the job needs a dedicated maintenance scope")
    for spec in specs:
        scope.require(spec.cat, spec.ver)


def _verified_token(keys: LegacyKeys, spec: TableSpec, token: TokenColumn, row, value) -> bool:
    old = row.get(token.column)
    if token.legacy_purpose is None:
        return not old  # the source supplies the original typed input directly
    if old:
        return any(
            hmac.compare_digest(
                bytes(old),
                cipher.dedup_mac(value)
                if token.dedup
                else cipher.blind_token(token.legacy_purpose, value),
            )
            for cipher in (keys.active, keys.previous)
            if cipher is not None
        )
    # Before the schema expansion the event lookup used the canonical payload hash.
    return (
        spec.table == "events"
        and isinstance(row.get("content_hash"), str)
        and (
            not token.dedup
            or hmac.compare_digest(row["content_hash"], hashlib.sha256(value).hexdigest())
        )
    )


def _legacy_value(spec: TableSpec, column: SealedColumn, row, keys: LegacyKeys) -> bytes | None:
    blob = row.get(column.column)
    if blob:
        key_id = row[spec.key_id_column] if spec.key_id_column else None
        return open_legacy(keys, bytes(blob), column.aad(row), family=column.family, key_id=key_id)
    if spec.table == "events" and column.column == "meta_enc" and "source" in row:
        return json.dumps(
            {
                "source": row["source"],
                "kind": row["kind"],
                "occurred_at": row["occurred_at"].isoformat(),
                "salience": float(row["salience_score"]),
                "memorable": bool(row["memorable"]),
                "extra": json.loads(row["meta"]) if row["meta"] else {},
            },
            ensure_ascii=False,
        ).encode("utf-8")
    if spec.table == "events" or blob == b"":
        raise MaintenanceRefused(f"{spec.table}.{column.column} has no valid legacy value")
    return None


async def migrate(
    source: asyncpg.Pool,
    target: asyncpg.Pool,
    *,
    scope: KeyScope,
    keys: LegacyKeys,
    job_id: bytes,
    store: VaultStore | None = None,
    tables: list[str] | None = None,
    audit_signer: Ed25519PublicKey | None = None,
    audit_signature: bytes | None = None,
) -> MigrationReport:
    """Copy supported records with atomic row/journal writes and verified resume.

    Explicit table selections are partial rehearsals. A full run refuses
    nonempty families with no conversion rule.
    """
    registry = {**SPECS, "audit_log": _AUDIT_SPEC}
    names = list(registry) if tables is None else tables
    if len(set(names)) != len(names) or any(name not in registry for name in names):
        raise MaintenanceRefused("the selected migration tables must be unique and registered")
    specs = [registry[name] for name in names]
    _check_scope(scope, specs)
    report = MigrationReport()
    journal = store or VaultStore(target)
    async with source.acquire() as old, target.acquire() as new:
        if await old.fetchval("SELECT current_database()") == await new.fetchval(
            "SELECT current_database()"
        ):
            raise MaintenanceRefused("source and shadow must have distinct database names")
        # The target is dedicated to one migration job at a time. A checked
        # out pool connection releases its session locks on reset.
        locked = await new.fetchval(
            "SELECT pg_try_advisory_lock(hashtextextended(current_schema(), 41307))"
        )
        if not locked:
            raise MaintenanceRefused("the shadow database already has a migration in progress")
        try:
            async with old.transaction():
                inventory = await _table_inventory(old)
                target_inventory = await _table_inventory(new)
                if "vault_migration_journal" not in target_inventory:
                    raise MaintenanceRefused("the shadow database needs the current schema")
                companions = {spec.tombstone for spec in specs} & inventory
                locked_tables = (
                    inventory if tables is None else (set(names) & inventory) | companions
                )
                for table in sorted(locked_tables):
                    await old.execute(f"LOCK TABLE {_quoted(table)} IN SHARE MODE NOWAIT")  # noqa: S608
                if tables is None:
                    for table in sorted(
                        inventory - set(registry) - {"schema_migrations", "forgotten_events"}
                    ):
                        await _refuse_rows(old, table, "has no approved conversion")
                    for table in sorted(
                        target_inventory
                        - set(registry)
                        - {"schema_migrations", "vault_migration_journal"}
                    ):
                        await _refuse_rows(new, table, "is outside the conversion")
                for table in sorted(companions):
                    await _refuse_rows(old, table, "contains unreconstructible tombstones")
                if "audit_log" in names:
                    await _archive_audit(
                        old,
                        new,
                        scope,
                        keys,
                        job_id,
                        report.for_table("audit_log"),
                        signer=audit_signer,
                        signature=audit_signature,
                    )
                for spec in specs:
                    if spec.table == "audit_log":
                        continue
                    await _migrate_table(
                        old,
                        new,
                        scope,
                        keys,
                        journal,
                        job_id,
                        spec,
                        report.for_table(spec.table),
                        exists=spec.table in inventory,
                    )
                _check_scope(scope, specs)
        finally:
            await new.fetchval(
                "SELECT pg_advisory_unlock(hashtextextended(current_schema(), 41307))"
            )
    return report


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


async def _refuse_rows(conn: asyncpg.Connection, table: str, reason: str) -> None:
    if await conn.fetchval(f"SELECT EXISTS (SELECT 1 FROM {_quoted(table)})"):  # noqa: S608
        raise MaintenanceRefused(f"{table} {reason}")


async def _migrate_table(
    old: asyncpg.Connection,
    new: asyncpg.Connection,
    scope: KeyScope,
    keys: LegacyKeys,
    journal: VaultStore,
    job_id: bytes,
    spec: TableSpec,
    report: TableReport,
    *,
    exists: bool,
) -> None:
    # All identifiers here belong to the checked registry.
    table = spec.table
    pk_name = spec.pk
    columns = await _columns(new, table)
    if not columns or any(
        classify(table, column) not in {"plaintext", "sealed"} for column in columns
    ):
        raise MaintenanceRefused(f"{table} target schema is unclassified")
    if SEALED_COLUMNS.get(table, frozenset()) != {column.column for column in spec.columns}:
        raise MaintenanceRefused(f"{table} has an incomplete sealed column spec")
    if exists and table == "identities":
        rows = await old.fetch(
            "WITH RECURSIVE lineage AS ("
            " SELECT identities.*, 0 AS depth FROM identities WHERE merged_into_id IS NULL"
            " UNION ALL SELECT child.*, parent.depth + 1 FROM identities AS child"
            " JOIN lineage AS parent ON child.merged_into_id = parent.id"
            ") SELECT * FROM lineage ORDER BY depth, id"
        )
        if len(rows) != await old.fetchval("SELECT count(*) FROM identities"):
            raise MaintenanceRefused("the identity merge references contain a cycle")
        rows = [{key: value for key, value in row.items() if key != "depth"} for row in rows]
    else:
        rows = await old.fetch(f"SELECT * FROM {table} ORDER BY {pk_name}") if exists else []  # noqa: S608
    report.rows = len(rows)
    storage = ScopedStore(scope, cat=spec.cat, ver=spec.ver)
    for row in rows:
        _check_scope(scope, [spec])
        pk = row[pk_name]
        entry_args = {
            "vault_id": scope.vault_id,
            "job_id": job_id,
            "table_name": table,
            "stable_id": stable_id(table, pk),
            "connection": new,
        }
        entry = await journal.journal_entry(**entry_args)
        existing = await new.fetchrow(f"SELECT * FROM {table} WHERE {pk_name} = $1", pk)  # noqa: S608
        if (entry is None) != (existing is None) or (
            entry is not None and entry["state"] != "done"
        ):
            raise MaintenanceRefused(f"{table} shadow row does not match its journal")
        copied = {column: row[column] for column in columns if column in row}
        if set(row.keys()) - set(columns) and spec.table != "events":
            raise MaintenanceRefused(f"{table} legacy schema needs a conversion rule")
        plains = {}
        for column in spec.columns:
            name = column.column
            raw = _legacy_value(spec, column, row, keys)
            copied[name] = None
            if raw is not None:
                plains[name] = _decode(raw, column.codec)
                row_id = bytes(pk).hex() if isinstance(pk, (bytes, bytearray)) else pk
                location = {"table": table, "column": name, "row_id": row_id}
                blob = (
                    existing[name] if existing is not None else storage.seal_bytes(raw, **location)
                )
                if not blob or storage.unseal_bytes(blob, **location) != raw:
                    raise MaintenanceRefused(f"{table}.{name} failed verification")
                copied[name] = blob
        for token in spec.tokens:
            try:
                value = token.derive(plains, row)
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                raise MaintenanceRefused(
                    f"{table}.{token.column} input cannot be reconstructed"
                ) from exc
            if not _verified_token(keys, spec, token, row, value):
                raise MaintenanceRefused(f"{table}.{token.column} input cannot be reconstructed")
            copied[token.column] = (
                scope.dedup(token.domain, value)
                if token.dedup
                else scope.token(token.domain, value)
            )
        if table == "processed_updates":
            copied["update_id"] = None
        for label in ("key_version", spec.key_id_column):
            if label and label in columns:
                copied[label] = f"v{spec.ver}"

        if existing is not None:
            if any(existing[name] != value for name, value in copied.items()):
                raise MaintenanceRefused(f"{table} shadow row does not match the source")
            report.resumed += 1
            continue

        placeholders = ", ".join(f"${index}" for index in range(1, len(copied) + 1))
        async with new.transaction():
            await new.execute(
                f"INSERT INTO {table} ({', '.join(copied)}) VALUES ({placeholders})",  # noqa: S608
                *copied.values(),
            )
            _check_scope(scope, [spec])
            await journal.journal(**entry_args, state="done")
            _check_scope(scope, [spec])
        report.migrated += 1

    if await new.fetchval(f"SELECT count(*) FROM {table}") != report.rows:  # noqa: S608
        raise MaintenanceRefused(f"{table} source and target counts differ")
    if exists:
        sequence = await new.fetchval("SELECT pg_get_serial_sequence($1, $2)", table, pk_name)
        if sequence is not None:
            source_last = await old.fetchval(
                "SELECT pg_sequence_last_value(pg_get_serial_sequence($1, $2)::regclass)",
                table,
                pk_name,
            )
            highest = max(source_last or 0, max((int(row[pk_name]) for row in rows), default=0))
            if highest:
                await new.fetchval(
                    "SELECT setval($1::text::regclass, $2::bigint)", sequence, highest
                )


async def _archive_audit(
    old: asyncpg.Connection,
    new: asyncpg.Connection,
    scope: KeyScope,
    keys: LegacyKeys,
    job_id: bytes,
    report: TableReport,
    *,
    signer: Ed25519PublicKey | None,
    signature: bytes | None,
) -> None:
    rows = await old.fetch("SELECT * FROM audit_log ORDER BY seq")
    existing = await new.fetch("SELECT * FROM audit_log ORDER BY seq")
    report.rows = len(rows)
    if not rows and not existing:
        return
    if not rows:
        raise MaintenanceRefused("the shadow audit chain has no source records")
    verified = verify_entries(rows)
    if (
        signer is None
        or signature is None
        or not matches_checkpoint(
            verified,
            vault_id=scope.vault_id,
            job_id=job_id,
            archived=False,
            signature=signature,
            signer=signer,
        )
    ):
        raise MaintenanceRefused("the source audit chain needs a matching signed checkpoint")

    records = []
    for row in rows:
        original = row.get("payload_enc")
        if original:
            entry = json.loads(open_legacy(keys, original, f"audit_log:payload_enc:{row['seq']}"))
            archive = {"ciphertext": original.hex()}
        else:
            canonical = legacy_canonical(row)
            entry = json.loads(canonical)
            archive = {"canonical": canonical}
        archive.update(entry_hash=row["entry_hash"], prev_hash=row["prev_hash"])
        records.append((row["seq"], row["created_at"], {**entry, "archived_entry": archive}))

    last = rows[-1]
    records.append(
        (
            last["seq"] + 1,
            last["created_at"],
            {
                "actor": "system",
                "decision": "info",
                "outcome": "Historical audit entries were sealed.",
                "legacy_head": verified.head_hash,
                "legacy_count": verified.entries,
                "source_checkpoint": signature.hex(),
            },
        )
    )
    storage = ScopedStore(scope, cat="operations", ver=1)
    if existing:
        if len(existing) != len(records) or not verify_entries(existing).ok:
            raise MaintenanceRefused("the shadow audit chain is incomplete or damaged")
        for row, (seq, created_at, expected) in zip(existing, records, strict=True):
            if (
                row["seq"] != seq
                or row["created_at"] != created_at
                or storage.unseal(
                    row["payload_enc"], table="audit_log", column="payload_enc", row_id=seq
                )
                != expected
            ):
                raise MaintenanceRefused("the shadow audit archive differs from the source")
        report.resumed = len(rows)
        return
    prev_hash = GENESIS_HASH
    async with new.transaction():
        for seq, created_at, entry in records:
            sealed = storage.seal(entry, table="audit_log", column="payload_enc", row_id=seq)
            if storage.unseal(sealed, table="audit_log", column="payload_enc", row_id=seq) != entry:
                raise MaintenanceRefused("an archived audit payload failed its round trip")
            entry_hash = compute_sealed_entry_hash(prev_hash, sealed)
            await new.execute(
                "INSERT INTO audit_log (seq, actor, tool_name, decision, prev_hash,"
                " entry_hash, created_at, payload_enc) VALUES ($1, '', '', '', $2, $3, $4, $5)",
                seq,
                prev_hash,
                entry_hash,
                created_at,
                sealed,
            )
            prev_hash = entry_hash
        copied = verify_entries(await new.fetch("SELECT * FROM audit_log ORDER BY seq"))
        if not copied.ok or copied.head_hash != prev_hash or copied.entries != len(records):
            raise MaintenanceRefused("the archived audit chain failed verification")
        scope.require("operations", 1)
    report.migrated = len(rows)
