"""Offline copying of verified legacy records into a separate AE2 database."""

from __future__ import annotations

import hashlib
import hmac
import json
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from getpass import GetPassWarning, getpass
from pathlib import Path
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
from ..workspace.store import _ENC_MAGIC, _FILE_BY_KEY, Workspace
from .scope import KeyScope, VaultLocked, historical_challenge
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


@dataclass(frozen=True)
class LegacyKeyIds:
    """Only nonsecret identifiers travel through the operator arguments."""

    active: str
    previous: str | None = None
    token: str | None = None
    previous_token: str | None = None


def read_legacy_keys(
    scope: KeyScope,
    *,
    active_id: str,
    previous_id: str | None = None,
    token_id: str | None = None,
    previous_token_id: str | None = None,
) -> LegacyKeys:
    """Read legacy material without allowing getpass to fall back to echoed input."""
    if scope.operation != "maintenance" or scope.closed or not scope.refs:
        raise MaintenanceRefused("legacy key input needs an open maintenance scope")
    if previous_id == active_id or (previous_token_id and not token_id):
        raise MaintenanceRefused("the legacy key identifiers are inconsistent")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", GetPassWarning)
            active = Cipher.from_b64(getpass("Legacy active content key: "), key_id=active_id)
            previous = (
                Cipher.from_b64(getpass("Legacy previous content key: "), key_id=previous_id)
                if previous_id
                else None
            )
            keyring = (
                TokenKeyring(
                    active_id=token_id,
                    active_key_b64=getpass("Legacy active token key: "),
                    previous_id=previous_token_id or "",
                    previous_key_b64=(
                        getpass("Legacy previous token key: ") if previous_token_id else ""
                    ),
                )
                if token_id
                else None
            )
    except (EOFError, OSError, GetPassWarning, CryptoError, TokenKeyError, ValueError):
        raise MaintenanceRefused("legacy keys could not be read from protected input") from None
    return LegacyKeys(active, previous, keyring)


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
    extra_keys: tuple[str, ...] = ()


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
    "commitment_reminders": TableSpec("commitment_reminders", "id", "memory", ()),
    "daily_counters": TableSpec("daily_counters", "day", "operations", ()),
    "workbench_minutes": TableSpec("workbench_minutes", "month", "operations", ()),
    "held_notes": _row_spec("held_notes", (("body_enc", "json"),)),
    "focus": TableSpec(
        "focus",
        "chat_ref_token",
        "memory",
        (
            SealedColumn(
                "state_enc",
                "json",
                lambda row: f"focus:state_enc:{bytes(row['chat_ref_token']).hex()}",
            ),
        ),
        (
            TokenColumn(
                "chat_ref_token",
                "focus.chat",
                "focus",
                lambda plains, _: plains["state_enc"]["lookup_chat"],
            ),
        ),
    ),
    "conclusions": TableSpec(
        "conclusions",
        "key_token",
        "memory",
        (
            SealedColumn(
                "value_enc",
                "json",
                lambda row: f"conclusions:value_enc:{bytes(row['key_token']).hex()}",
            ),
        ),
        (
            TokenColumn(
                "key_token",
                "conclusion.key",
                "conclusion",
                lambda plains, _: plains["value_enc"]["lookup_key"],
            ),
        ),
    ),
    "pending_approvals": _row_spec(
        "pending_approvals",
        (("params_enc", "json"), ("rules_matched_enc", "text"), ("note_enc", "text")),
    ),
    "scheduled_actions": _row_spec(
        "scheduled_actions", (("payload_enc", "json"), ("meta_enc", "json"))
    ),
    "alerts": _row_spec(
        "alerts",
        (("spec_enc", "json"), ("origin_enc", "json")),
        tokens=(
            TokenColumn(
                "scope_token",
                "alert.scope",
                "alert_scope",
                lambda plains, _: str(plains["spec_enc"].get("scope", "")),
            ),
        ),
    ),
    "alert_notifications": _row_spec(
        "alert_notifications",
        (("event_enc", "json"),),
        tokens=(
            TokenColumn(
                "key_token",
                "alert.notification",
                "alert_notification",
                lambda plains, _: (
                    plains["event_enc"].get("dedup_key")
                    or f"{plains['event_enc']['alert_id']}:{plains['event_enc']['event_id']}"
                ),
            ),
        ),
    ),
    "inbound_messages": _row_spec(
        "inbound_messages",
        (("reason_enc", "text"), ("outcome_enc", "text"), ("identity_enc", "json")),
        tokens=(
            TokenColumn(
                "chat_token",
                "inbound.chat",
                "inbound-chat",
                lambda plains, _: plains["identity_enc"]["chat"],
            ),
            TokenColumn(
                "message_token",
                "inbound.message",
                "inbound-message",
                lambda plains, _: plains["identity_enc"]["message"],
            ),
            TokenColumn(
                "key_token",
                "inbound.key",
                "inbound-key",
                lambda plains, _: plains["identity_enc"]["key"],
            ),
        ),
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
    "provider_sync_state": TableSpec(
        "provider_sync_state",
        "provider",
        "credentials",
        (
            SealedColumn(
                "value_enc",
                "text",
                lambda row: (
                    f"provider_sync_state:value_enc:{row['provider']}\x1f"
                    f"{row['app']}\x1f{row['key']}"
                ),
            ),
        ),
        extra_keys=("app", "key"),
    ),
    "proactive_tasks": _row_spec(
        "proactive_tasks",
        (
            ("label_enc", "text"),
            ("meta_enc", "json"),
            ("opportunity_enc", "json"),
            ("plan_enc", "json"),
            ("result_enc", "text"),
            ("feedback_note_enc", "text"),
        ),
        tokens=(
            TokenColumn(
                "opportunity_token",
                "proactive.opportunity",
                "opportunity",
                lambda plains, _: (
                    f"{plains['meta_enc']['skill']}\x1f{plains['meta_enc']['opportunity_key']}"
                ),
            ),
            TokenColumn(
                "day_token",
                "proactive.day",
                "day",
                lambda plains, _: plains["meta_enc"]["day"],
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


async def migrate_workspace_disk(
    target: asyncpg.Pool,
    root: str | Path,
    *,
    scope: KeyScope,
    keys: LegacyKeys,
    job_id: bytes,
) -> TableReport:
    """Copy a legacy disk workspace into the encrypted shadow, retaining files."""
    _check_scope(scope, [SPECS["workspace_files"]])
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise MaintenanceRefused("the legacy workspace directory is unavailable")
    names = set(_FILE_BY_KEY.values())
    files: list[tuple[str, Path]] = []
    for entry in sorted(root.iterdir()):
        if entry.is_symlink():
            raise MaintenanceRefused("the legacy workspace contains a symbolic link")
        if entry.name == "memory" and entry.is_dir():
            for item in sorted(entry.iterdir()):
                if item.is_symlink() or not item.is_file() or item.suffix != ".md":
                    raise MaintenanceRefused("the legacy workspace contains an unknown item")
                try:
                    from datetime import date

                    if date.fromisoformat(item.stem).isoformat() != item.stem:
                        raise ValueError
                except ValueError:
                    raise MaintenanceRefused(
                        "a legacy daily workspace filename is invalid"
                    ) from None
                files.append((f"memory/{item.name}", item))
        elif entry.name in names and entry.is_file():
            files.append((entry.name, entry))
        else:
            raise MaintenanceRefused("the legacy workspace contains an unknown item")
    workspace = Workspace.scoped(target, scope)
    journal = VaultStore(target)
    report = TableReport(table="workspace_files", rows=len(files))
    async with target.acquire() as connection:
        locked = await connection.fetchval(
            "SELECT pg_try_advisory_lock(hashtextextended(current_schema(), 41307))"
        )
        if not locked:
            raise MaintenanceRefused("the shadow database already has a migration in progress")
        try:
            for name, path in files:
                _check_scope(scope, [SPECS["workspace_files"]])
                raw = path.read_bytes()
                if raw.startswith(_ENC_MAGIC):
                    raw = open_legacy(keys, raw[len(_ENC_MAGIC) :], f"workspace:file:{name}")
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError:
                    raise MaintenanceRefused("a legacy workspace file is not UTF-8") from None
                stable = scope.token("workspace.name", name)
                entry = await journal.journal_entry(
                    vault_id=scope.vault_id,
                    job_id=job_id,
                    table_name="workspace_files",
                    stable_id=stable,
                    connection=connection,
                )
                row = await workspace._row(name)
                if row is not None:
                    if await workspace._read_name(name) != content:
                        raise MaintenanceRefused("a workspace file conflicts with the shadow")
                    report.resumed += 1
                else:
                    if entry is not None:
                        raise MaintenanceRefused("a workspace journal entry has no matching row")
                    await workspace._put(name, content)
                    if await workspace._read_name(name) != content:
                        raise MaintenanceRefused("a workspace file failed its round trip")
                    report.migrated += 1
                _check_scope(scope, [SPECS["workspace_files"]])
                await journal.journal(
                    vault_id=scope.vault_id,
                    job_id=job_id,
                    table_name="workspace_files",
                    stable_id=stable,
                    state="done",
                    connection=connection,
                )
        finally:
            await connection.fetchval(
                "SELECT pg_advisory_unlock(hashtextextended(current_schema(), 41307))"
            )
    return report


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
        try:
            return open_legacy(
                keys, bytes(blob), column.aad(row), family=column.family, key_id=key_id
            )
        except MaintenanceRefused:
            if spec.table != "alerts" or column.column != "spec_enc":
                raise
            return open_legacy(keys, bytes(blob), f"watches:spec_enc:{row['id']}")
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
    keys: LegacyKeys | None = None,
    key_ids: LegacyKeyIds | None = None,
    job_id: bytes,
    store: VaultStore | None = None,
    tables: list[str] | None = None,
    audit_signer: Ed25519PublicKey | None = None,
    audit_signature: bytes | None = None,
    historical_source_complete: bool = False,
) -> MigrationReport:
    """Copy supported records with atomic row/journal writes and verified resume.

    Explicit table selections are partial rehearsals. A full run refuses
    nonempty families with no conversion rule. Set historical_source_complete
    only after independently reconciling all source key versions with the
    provided active and previous keys.
    """
    registry = {**SPECS, "audit_log": _AUDIT_SPEC}
    names = list(registry) if tables is None else tables
    if len(set(names)) != len(names) or any(name not in registry for name in names):
        raise MaintenanceRefused("the selected migration tables must be unique and registered")
    specs = [registry[name] for name in names]
    _check_scope(scope, specs)
    if (keys is None) == (key_ids is None):
        raise MaintenanceRefused("supply either legacy keys or protected input identifiers")
    if keys is None:
        assert key_ids is not None
        keys = read_legacy_keys(
            scope,
            active_id=key_ids.active,
            previous_id=key_ids.previous,
            token_id=key_ids.token,
            previous_token_id=key_ids.previous_token,
        )
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
                        - {
                            "schema_migrations",
                            "vault_migration_journal",
                            "vault_state",
                            "vault_manifest",
                            "vault_aead_budget",
                            "historical_lookup_requirements",
                            "forgotten_events",
                        }
                    ):
                        await _refuse_rows(new, table, "is outside the conversion")
                if "forgotten_events" in companions:
                    await _migrate_forgotten(
                        old, new, scope, keys, historical_source_complete, report, journal, job_id
                    )
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
                        history_confirmed=historical_source_complete,
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


async def _migrate_forgotten(
    old: asyncpg.Connection,
    new: asyncpg.Connection,
    scope: KeyScope,
    keys: LegacyKeys,
    history_confirmed: bool,
    report: MigrationReport,
    journal: VaultStore,
    job_id: bytes,
) -> None:
    query = "SELECT dedup_mac, source_token, forgotten_at FROM forgotten_events ORDER BY dedup_mac"
    rows = await old.fetch(query)
    if not rows:
        await _refuse_rows(new, "forgotten_events", "has no matching source tombstones")
        return
    proofs = _historical_proofs(scope, keys, "event.dedup", None, history_confirmed)
    if any(len(row["dedup_mac"]) != 32 or len(row["source_token"]) != 32 for row in rows):
        raise MaintenanceRefused("forgotten events contain a malformed legacy tag")

    info = report.for_table("forgotten_events")
    info.rows = len(rows)
    async with new.transaction():
        await _save_proofs(new, "event.dedup", proofs)
        current = await new.fetch(query)
        if current:
            if current != rows:
                raise MaintenanceRefused("shadow tombstones do not match the source")
            info.resumed = len(rows)
        else:
            await new.executemany(
                "INSERT INTO forgotten_events (dedup_mac, source_token, forgotten_at)"
                " VALUES ($1, $2, $3)",
                [tuple(row.values()) for row in rows],
            )
            if await new.fetch(query) != rows:
                raise MaintenanceRefused("shadow tombstones do not match the source")
            info.migrated = len(rows)
        for row in rows:
            args = {
                "vault_id": scope.vault_id,
                "job_id": job_id,
                "table_name": "forgotten_events",
                "stable_id": stable_id("forgotten_events", row["dedup_mac"].hex()),
                "connection": new,
            }
            entry = await journal.journal_entry(**args)
            if current:
                if entry is None or entry["state"] != "done":
                    raise MaintenanceRefused("shadow tombstone does not match its journal")
            else:
                if entry is not None:
                    raise MaintenanceRefused("tombstone journal entry has no matching shadow row")
                await journal.journal(**args, state="done")
        scope.require_historical("event.dedup", proofs)


def _historical_proofs(
    scope: KeyScope, keys: LegacyKeys, domain: str, purpose: str | None, confirmed: bool
) -> list[bytes]:
    if not confirmed:
        raise MaintenanceRefused("historical records need independently confirmed key coverage")
    challenge = historical_challenge(scope.vault_id, domain)
    proofs = sorted(
        {
            cipher.dedup_mac(challenge)
            if purpose is None
            else cipher.blind_token(purpose, challenge)
            for cipher in (keys.active, keys.previous)
            if cipher is not None
        }
    )
    try:
        scope.require_historical(domain, proofs)
    except VaultLocked:
        raise MaintenanceRefused(
            "the maintenance grant is missing a historical lookup key"
        ) from None
    return proofs


async def _save_proofs(conn: asyncpg.Connection, domain: str, proofs: list[bytes]) -> None:
    await conn.execute(
        "INSERT INTO historical_lookup_requirements (domain, proofs) VALUES ($1, $2)"
        " ON CONFLICT (domain) DO NOTHING",
        domain,
        proofs,
    )
    if (
        await conn.fetchval(
            "SELECT proofs FROM historical_lookup_requirements WHERE domain = $1", domain
        )
        != proofs
    ):
        raise MaintenanceRefused("shadow historical lookup requirements do not match the source")


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
    history_confirmed: bool,
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
        order = ", ".join((pk_name, *spec.extra_keys))
        rows = await old.fetch(f"SELECT * FROM {table} ORDER BY {order}") if exists else []  # noqa: S608
    report.rows = len(rows)
    storage = ScopedStore(scope, cat=spec.cat, ver=spec.ver)
    for row in rows:
        _check_scope(scope, [spec])
        primary_keys = (pk_name, *spec.extra_keys)
        key_values = tuple(row[name] for name in primary_keys)
        pk = row[pk_name]
        row_id = bytes(pk).hex() if isinstance(pk, (bytes, bytearray)) else pk
        journal_id = pk
        target_key_values = key_values
        retained: dict[str, list[bytes]] = {}
        if table in ("focus", "conclusions"):
            column = spec.columns[0]
            raw = _legacy_value(spec, column, row, keys)
            if raw is None:
                raise MaintenanceRefused(f"{table} lookup input cannot be reconstructed")
            body = _decode(raw, column.codec)
            token = spec.tokens[0]
            try:
                value = token.derive({column.column: body}, row)
            except KeyError:
                if not history_confirmed or not isinstance(pk, bytes) or len(pk) != 32:
                    raise MaintenanceRefused(
                        f"{table} lookup input cannot be reconstructed"
                    ) from None
                retained[token.domain] = _historical_proofs(
                    scope, keys, token.domain, token.legacy_purpose, history_confirmed
                )
            else:
                if not isinstance(value, str) or not _verified_token(keys, spec, token, row, value):
                    raise MaintenanceRefused(f"{table} lookup input cannot be reconstructed")
                target_pk = scope.token(token.domain, value)
                target_key_values = (target_pk,)
                row_id = target_pk.hex()
        if spec.extra_keys:
            joined = "\x1f".join(key_values)
            row_id = hashlib.sha256(joined.encode()).hexdigest()
            journal_id = json.dumps(key_values, ensure_ascii=False, separators=(",", ":"))
        entry_args = {
            "vault_id": scope.vault_id,
            "job_id": job_id,
            "table_name": table,
            "stable_id": stable_id(table, journal_id),
            "connection": new,
        }
        entry = await journal.journal_entry(**entry_args)
        condition = " AND ".join(
            f"{name} = ${index}" for index, name in enumerate(primary_keys, start=1)
        )
        existing = await new.fetchrow(
            f"SELECT * FROM {table} WHERE {condition}", *target_key_values
        )  # noqa: S608
        if (entry is None) != (existing is None) or (
            entry is not None and entry["state"] != "done"
        ):
            raise MaintenanceRefused(f"{table} shadow row does not match its journal")
        copied = {column: row[column] for column in columns if column in row}
        if table in ("focus", "conclusions"):
            copied[pk_name] = target_key_values[0]
        if set(row.keys()) - set(columns) and spec.table != "events":
            raise MaintenanceRefused(f"{table} legacy schema needs a conversion rule")
        plains = {}
        for column in spec.columns:
            name = column.column
            raw = _legacy_value(spec, column, row, keys)
            copied[name] = None
            if raw is not None:
                plains[name] = _decode(raw, column.codec)
                location = {"table": table, "column": name, "row_id": row_id}
                blob = (
                    existing[name] if existing is not None else storage.seal_bytes(raw, **location)
                )
                if not blob or storage.unseal_bytes(blob, **location) != raw:
                    raise MaintenanceRefused(f"{table}.{name} failed verification")
                copied[name] = blob
        for token in spec.tokens:
            if token.domain in retained:
                continue
            if table == "inbound_messages" and "identity_enc" not in plains and history_confirmed:
                old_tag = row.get(token.column)
                if not isinstance(old_tag, bytes) or len(old_tag) != 32:
                    raise MaintenanceRefused("inbound message has a malformed historical tag")
                retained[token.domain] = _historical_proofs(
                    scope, keys, token.domain, token.legacy_purpose, history_confirmed
                )
                continue
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
            for domain, proofs in retained.items():
                if (
                    await new.fetchval(
                        "SELECT proofs FROM historical_lookup_requirements WHERE domain = $1",
                        domain,
                    )
                    != proofs
                ):
                    raise MaintenanceRefused("shadow historical lookup requirements are missing")
            report.resumed += 1
            continue

        placeholders = ", ".join(f"${index}" for index in range(1, len(copied) + 1))
        async with new.transaction():
            for domain, proofs in retained.items():
                await _save_proofs(new, domain, proofs)
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
    if exists and not spec.extra_keys:
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
