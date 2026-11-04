"""The plaintext allowlist and sealed-column registry.

Kept free of database imports so the CI guard can run in the lint job,
before the package's runtime dependencies are installed. `memory/storage.py`
owns the async side (the boot audit and the sealed write path) and
re-exports everything here.

The rule: only opaque ids, sequence numbers, key
versions, ciphertext, keyed blind-index tokens, status vocabulary and
schema bookkeeping may sit in a plain column. Everything the user wrote,
received, or that was derived from it is sealed.

`LEGACY_PLAINTEXT` is the migration ledger: columns that are still
plaintext today and must be sealed and dropped before the audit runs in
enforce mode. Keeping it in code means the boot audit and the CI check
agree about what is left.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# key_version is written automatically by SealedStore
KEY_VERSION = "key_version"

# Final-state plaintext columns. A column listed here may be written and
# read without sealing because it is opaque, a keyed token, a status
# vocabulary, a counter, or storage bookkeeping.
PLAINTEXT_ALLOWLIST: dict[str, frozenset[str]] = {
    "events": frozenset(
        {
            "id",
            "key_version",
            "source_token",
            "dedup_mac",
            "indexed_at",
            "created_at",
        }
    ),
    "identities": frozenset({"id", "created_at", "merged_into_id", "key_version"}),
    "identity_handles": frozenset(
        {"id", "identity_id", "platform_token", "normalized_token", "created_at", "key_version"}
    ),
    "entity_notes": frozenset({"id", "identity_id", "created_at", "key_version"}),
    "chat_messages": frozenset({"id", "direction", "surface_token", "created_at", "key_version"}),
    "pending_approvals": frozenset(
        {
            "id",
            "tool_name",
            "status",
            "created_at",
            "decided_at",
            "expires_at",
            "decided_by",
            "media_id",
            "key_version",
        }
    ),
    "scheduled_actions": frozenset({"id", "status", "result_digest", "created_at", "key_version"}),
    "audit_log": frozenset(
        {
            "seq",
            "actor",
            "tool_name",
            "decision",
            "rules_matched",
            "params_digest",
            "outcome",
            "prev_hash",
            "entry_hash",
            "created_at",
        }
    ),
    "connector_cursors": frozenset({"source", "key_version"}),
    "agent_settings": frozenset({"id", "updated_at", "key_version"}),
    "routines": frozenset(
        {
            "id",
            "enabled",
            "cooldown_seconds",
            "fire_count",
            "last_fired_at",
            "created_at",
            "key_version",
        }
    ),
    "decision_traces": frozenset({"id", "kind", "created_at", "key_version"}),
    "config_overrides": frozenset({"path", "updated_at", "key_version"}),
    "oauth_tokens": frozenset({"provider", "scopes", "expires_at", "updated_at", "key_version"}),
    "app_secrets": frozenset({"name", "updated_at", "key_version"}),
    "memory_chunks": frozenset(
        {
            "id",
            "event_id",
            "key_version",
            "embed_model_token",
            "dims",
            # position inside its own event — retrieval needs the chunk
            # before and after a hit, and row ids interleave across events
            # under concurrent ingest, so id order is not document order
            "ordinal",
            "created_at",
        }
    ),
    "daily_counters": frozenset(
        {
            "day",
            "searches",
            "tasks",
            "tokens",
            "sandbox_seconds",
            "notifications",
            "images",
            "sandbox_jobs",
            "updated_at",
        }
    ),
    "workbench_transfers": frozenset(
        {"id", "token_hash", "kind", "used", "expires_at", "created_at", "key_version"}
    ),
    "workbench_minutes": frozenset({"month", "seconds", "updated_at"}),
    "proactive_tasks": frozenset(
        {
            "id",
            "skill",
            "status",
            "feedback",
            "provider",
            "day_token",
            "opportunity_token",
            "created_at",
            "updated_at",
            "media_id",
            "key_version",
        }
    ),
    "media_blobs": frozenset(
        {"id", "size", "dedup_mac", "origin_token", "created_at", "last_used_at", "key_version"}
    ),
    "processed_updates": frozenset({"id", "source", "update_id", "created_at"}),
    "provider_connections": frozenset(
        {
            "provider",
            "auth_mode",
            "token_type",
            "scopes",
            "key_id",
            "expires_at",
            "refresh_expires_at",
            "status",
            "last_refreshed_at",
            "created_at",
            "updated_at",
            "key_version",
        }
    ),
    "provider_oauth_states": frozenset(
        {
            "state_hash",
            "provider",
            "app",
            "scopes",
            "key_id",
            "redirect_uri",
            "created_at",
            "expires_at",
            "consumed_at",
        }
    ),
    "provider_sync_state": frozenset({"provider", "app", "key", "updated_at", "key_version"}),
    "schema_migrations": frozenset({"version", "applied_at"}),
    # tables introduced by the sealed storage phase
    "facts": frozenset(
        {"id", "subject_identity_id", "key_token", "status", "created_at", "key_version"}
    ),
    "commitments": frozenset(
        {
            "id",
            "kind",
            "status",
            "counterparty_identity_id",
            "source_event_id",
            "created_at",
            "key_version",
        }
    ),
    "watches": frozenset(
        {
            "id",
            "scope_token",
            "channel",
            "status",
            "last_run_at",
            "last_notified_at",
            "created_at",
            "key_version",
        }
    ),
    "summaries": frozenset({"id", "kind", "period_token", "created_at", "key_version"}),
    "focus": frozenset({"chat_ref_token", "updated_at", "key_version"}),
    "conclusions": frozenset({"key_token", "created_at", "key_version"}),
    "deliveries": frozenset({"id", "surface_token", "status", "created_at", "key_version"}),
    "workspace_files": frozenset({"id", "name_token", "updated_at", "key_version"}),
}

# Columns that must be ciphertext. The boot audit also checks their SQL
# type is bytea, so a migration cannot silently leave one as text.
SEALED_COLUMNS: dict[str, frozenset[str]] = {
    "events": frozenset({"payload_enc", "meta_enc"}),
    "identities": frozenset({"profile_enc"}),
    "identity_handles": frozenset({"handle_enc"}),
    "entity_notes": frozenset({"payload_enc"}),
    "chat_messages": frozenset({"payload_enc"}),
    "pending_approvals": frozenset({"params_enc", "rules_matched_enc", "note_enc"}),
    "scheduled_actions": frozenset({"payload_enc", "meta_enc"}),
    "agent_settings": frozenset({"personality_enc"}),
    "routines": frozenset({"label_enc", "trigger_enc", "action_enc"}),
    "decision_traces": frozenset({"label_enc", "trace_enc"}),
    "config_overrides": frozenset({"value_enc"}),
    "oauth_tokens": frozenset({"access_enc", "refresh_enc"}),
    "app_secrets": frozenset({"value_enc"}),
    "memory_chunks": frozenset({"text_enc", "embedding_enc", "meta_enc"}),
    "workbench_transfers": frozenset({"payload_enc"}),
    "proactive_tasks": frozenset(
        {
            "label_enc",
            "meta_enc",
            "opportunity_enc",
            "plan_enc",
            "result_enc",
            "feedback_note_enc",
        }
    ),
    "media_blobs": frozenset({"bytes_enc", "meta_enc"}),
    "provider_connections": frozenset(
        {"access_enc", "refresh_enc", "account_label_enc", "account_id_enc", "last_error_enc"}
    ),
    "provider_oauth_states": frozenset({"code_verifier_enc"}),
    "provider_sync_state": frozenset({"value_enc"}),
    "connector_cursors": frozenset({"last_seen_enc"}),
    "facts": frozenset({"body_enc"}),
    "commitments": frozenset({"body_enc"}),
    "watches": frozenset({"spec_enc"}),
    "summaries": frozenset({"body_enc"}),
    "focus": frozenset({"state_enc"}),
    "conclusions": frozenset({"value_enc"}),
    "deliveries": frozenset({"body_enc", "message_id_enc", "error_enc"}),
    "workspace_files": frozenset({"name_enc", "content_enc"}),
}

# The migration ledger is now empty: `018_sealed_cutover.sql` dropped every
# plaintext original. Historical note for anyone reading an old database:
# the cutover covered events (source, kind, occurred_at, content_hash,
# salience_score, memorable, meta), identities (display_name, confidence),
# identity_handles (platform, raw_handle, normalized), chat_messages
# (surface), pending_approvals (rules_matched, note), scheduled_actions
# (label, run_at), routines (label), decision_traces (label), memory_chunks
# (source, kind, filename, location, occurred_at, embed_model, embedding,
# norm), proactive_tasks (label, opportunity_key, day), media_blobs
# (filename, media_type, width, height, duration_s, origin),
# provider_connections (account_label, account_id, last_error),
# provider_sync_state (value) and connector_cursors (last_seen).
LEGACY_PLAINTEXT: dict[str, frozenset[str]] = {}


class StorageGuardError(RuntimeError):
    """A schema or write violated the sealed-storage contract."""


def classify(table: str, column: str) -> str:
    """One column's class: plaintext, sealed, legacy, or unknown."""
    check_identifier(table)
    check_identifier(column)
    if column in PLAINTEXT_ALLOWLIST.get(table, frozenset()):
        return "plaintext"
    if column in SEALED_COLUMNS.get(table, frozenset()):
        return "sealed"
    if column in LEGACY_PLAINTEXT.get(table, frozenset()):
        return "legacy"
    if table not in PLAINTEXT_ALLOWLIST and table not in SEALED_COLUMNS:
        return "unknown_table"
    return "unknown"


def check_identifier(name: str) -> None:
    if not IDENTIFIER_RE.match(name or ""):
        raise StorageGuardError(f"not a safe SQL identifier: {name!r}")


def check_write(table: str, columns: Iterable[str]) -> None:
    """Refuse a plaintext write to anything that is sealed or unknown."""
    for column in columns:
        kind = classify(table, column)
        if kind == "sealed":
            raise StorageGuardError(
                f"{table}.{column} is sealed; write it through the sealed path, not plaintext"
            )
        if kind in {"unknown", "unknown_table", "legacy"}:
            raise StorageGuardError(
                f"{table}.{column} is {kind}: classify it before writing through the guard"
            )


def audit_columns(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """Pure schema audit. Rows are information_schema.columns records."""
    violations: list[str] = []
    for row in rows:
        table = str(row["table_name"])
        column = str(row["column_name"])
        data_type = str(row.get("data_type", ""))
        if table not in PLAINTEXT_ALLOWLIST and table not in SEALED_COLUMNS:
            violations.append(f"{table}: table is not in the storage registry")
            continue
        kind = classify(table, column)
        if kind == "plaintext":
            continue
        if kind == "sealed":
            if data_type != "bytea":
                violations.append(f"{table}.{column}: sealed column is {data_type}, not bytea")
            continue
        if kind == "legacy":
            violations.append(f"{table}.{column}: legacy plaintext column still present")
            continue
        violations.append(f"{table}.{column}: column is not classified")
    return violations
