"""Seal the plaintext columns in place.

The expand migration (`017_sealed_storage.sql`) adds the sealed columns; this
command fills them from the plaintext copy, verifies the round trip, and
leaves the plaintext in place until the cutover migration drops it. It is
idempotent (each job selects rows whose sealed target is still null) and
resumable (a crash or a failed row is retried on the next run).

Usage, against the deployment's own database:

    python -m aether.memory.seal_migrate              # dry run: pending counts
    python -m aether.memory.seal_migrate --apply      # seal every table
    python -m aether.memory.seal_migrate --verify-only

It never prints content — only table names and counts. Run it with the same
key and key id the application runs with. A row whose plaintext cannot be
decrypted is left alone and counted as failed, never replaced with a guess.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import asyncpg

from ..config import Settings
from ..llm.embeddings import pack_vector, unpack_vector
from .crypto import Cipher, CryptoError
from .db import create_pool
from .events import canonical_payload
from .storage import KEY_VERSION

log = logging.getLogger("aether.memory.seal")

# The columns `018_sealed_cutover.sql` drops. Kept here so the CLI can say
# "already cut over" instead of running queries against a gone column.
LEGACY_COLUMNS: dict[str, frozenset[str]] = {
    "events": frozenset(
        {"source", "kind", "occurred_at", "content_hash", "salience_score", "memorable", "meta"}
    ),
    "identities": frozenset({"display_name", "confidence"}),
    "identity_handles": frozenset({"platform", "raw_handle", "normalized"}),
    "chat_messages": frozenset({"surface"}),
    "pending_approvals": frozenset({"rules_matched", "note"}),
    "scheduled_actions": frozenset({"label", "run_at"}),
    "routines": frozenset({"label"}),
    "decision_traces": frozenset({"label"}),
    "memory_chunks": frozenset(
        {
            "source",
            "kind",
            "filename",
            "location",
            "occurred_at",
            "embed_model",
            "embedding",
            "norm",
        }
    ),
    "proactive_tasks": frozenset({"label", "opportunity_key", "day"}),
    "media_blobs": frozenset({"filename", "media_type", "width", "height", "duration_s", "origin"}),
    "provider_connections": frozenset({"account_label", "account_id", "last_error"}),
    "provider_sync_state": frozenset({"value"}),
    "connector_cursors": frozenset({"last_seen"}),
}

Apply = Callable[[asyncpg.Connection, Cipher, asyncpg.Record], Awaitable[bool]]


@dataclass
class TableResult:
    table: str
    sealed: int = 0
    failed: int = 0
    remaining: int = 0


@dataclass(frozen=True)
class Job:
    table: str
    columns: str
    where: str
    apply: Apply
    order_by: str = "id"

    @property
    def select_sql(self) -> str:
        return (
            f"SELECT {self.columns} FROM {self.table}"
            f" WHERE {self.where} ORDER BY {self.order_by} LIMIT $1"
        )

    @property
    def count_sql(self) -> str:
        return f"SELECT COUNT(*) FROM {self.table} WHERE {self.where}"


def _seal(cipher: Cipher, value: object, table: str, column: str, pk: object) -> bytes:
    """Seal and prove the round trip before the ciphertext is written."""
    blob = cipher.encrypt_json(value, aad=f"{table}:{column}:{pk}")
    if cipher.decrypt_json(blob, aad=f"{table}:{column}:{pk}") != value:
        raise CryptoError(f"{table}.{column} row {pk}: round trip did not match")
    return blob


def _seal_text(cipher: Cipher, value: str, table: str, column: str, pk: object) -> bytes:
    blob = cipher.encrypt_text(value, aad=f"{table}:{column}:{pk}")
    if cipher.decrypt_text(blob, aad=f"{table}:{column}:{pk}") != value:
        raise CryptoError(f"{table}.{column} row {pk}: round trip did not match")
    return blob


def _iso(value: object) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


# ---------------------------------------------------------------------------
# one apply function per table
# ---------------------------------------------------------------------------


async def _seal_event(conn: asyncpg.Connection, cipher: Cipher, row: asyncpg.Record) -> bool:
    try:
        payload = cipher.decrypt_json(row["payload_enc"], aad=f"events:payload_enc:{row['id']}")
    except CryptoError:
        return False  # never seal an empty stand-in over unreadable content
    meta = {
        "source": row["source"],
        "kind": row["kind"],
        "occurred_at": _iso(row["occurred_at"]),
        "salience": float(row["salience_score"]),
        "memorable": bool(row["memorable"]),
        "extra": json.loads(row["meta"]) if row["meta"] else {},
    }
    await conn.execute(
        "UPDATE events SET source_token = $1, dedup_mac = $2, meta_enc = $3,"
        f" {KEY_VERSION} = $4 WHERE id = $5",
        cipher.blind_token("source", row["source"]),
        cipher.dedup_mac(canonical_payload(row["kind"], payload)),
        _seal(cipher, meta, "events", "meta_enc", row["id"]),
        cipher.key_id,
        row["id"],
    )
    return True


async def _seal_identity(conn: asyncpg.Connection, cipher: Cipher, row: asyncpg.Record) -> bool:
    profile = {
        "display_name": row["display_name"],
        "confidence": float(row["confidence"]),
    }
    await conn.execute(
        f"UPDATE identities SET profile_enc = $1, {KEY_VERSION} = $2 WHERE id = $3",
        _seal(cipher, profile, "identities", "profile_enc", row["id"]),
        cipher.key_id,
        row["id"],
    )
    return True


async def _seal_handle(conn: asyncpg.Connection, cipher: Cipher, row: asyncpg.Record) -> bool:
    await conn.execute(
        "UPDATE identity_handles SET handle_enc = $1, platform_token = $2,"
        f" normalized_token = $3, {KEY_VERSION} = $4 WHERE id = $5",
        _seal(
            cipher,
            {"platform": row["platform"], "handle": row["raw_handle"]},
            "identity_handles",
            "handle_enc",
            row["id"],
        ),
        cipher.blind_token("platform", row["platform"]),
        cipher.blind_token("handle", row["normalized"]),
        cipher.key_id,
        row["id"],
    )
    return True


async def _seal_chat(conn: asyncpg.Connection, cipher: Cipher, row: asyncpg.Record) -> bool:
    await conn.execute(
        f"UPDATE chat_messages SET surface_token = $1, {KEY_VERSION} = $2 WHERE id = $3",
        cipher.blind_token("source", row["surface"]),
        cipher.key_id,
        row["id"],
    )
    return True


async def _seal_approval(conn: asyncpg.Connection, cipher: Cipher, row: asyncpg.Record) -> bool:
    await conn.execute(
        "UPDATE pending_approvals SET rules_matched_enc = $1, note_enc = $2,"
        f" {KEY_VERSION} = $3 WHERE id = $4",
        _seal_text(
            cipher, row["rules_matched"], "pending_approvals", "rules_matched_enc", row["id"]
        ),
        _seal_text(cipher, row["note"], "pending_approvals", "note_enc", row["id"]),
        cipher.key_id,
        row["id"],
    )
    return True


async def _seal_scheduled(conn: asyncpg.Connection, cipher: Cipher, row: asyncpg.Record) -> bool:
    meta = {"label": row["label"], "run_at": _iso(row["run_at"])}
    await conn.execute(
        f"UPDATE scheduled_actions SET meta_enc = $1, {KEY_VERSION} = $2 WHERE id = $3",
        _seal(cipher, meta, "scheduled_actions", "meta_enc", row["id"]),
        cipher.key_id,
        row["id"],
    )
    return True


async def _seal_routine(conn: asyncpg.Connection, cipher: Cipher, row: asyncpg.Record) -> bool:
    await conn.execute(
        f"UPDATE routines SET label_enc = $1, {KEY_VERSION} = $2 WHERE id = $3",
        _seal_text(cipher, row["label"], "routines", "label_enc", row["id"]),
        cipher.key_id,
        row["id"],
    )
    return True


async def _seal_trace(conn: asyncpg.Connection, cipher: Cipher, row: asyncpg.Record) -> bool:
    await conn.execute(
        f"UPDATE decision_traces SET label_enc = $1, {KEY_VERSION} = $2 WHERE id = $3",
        _seal_text(cipher, row["label"], "decision_traces", "label_enc", row["id"]),
        cipher.key_id,
        row["id"],
    )
    return True


async def _seal_chunk(conn: asyncpg.Connection, cipher: Cipher, row: asyncpg.Record) -> bool:
    legacy_embedding = None
    if row["embedding"] is not None:
        legacy_embedding = list(row["embedding"])
        dims = len(legacy_embedding)
    elif row["embedding_enc"] is not None:
        try:
            dims = len(
                unpack_vector(
                    cipher.decrypt_bytes(
                        row["embedding_enc"], aad=f"chunks:embedding_enc:{row['id']}"
                    )
                )
            )
        except CryptoError:
            return False
    else:
        dims = None
    meta = {
        "source": row["source"],
        "kind": row["kind"],
        "filename": row["filename"],
        "location": row["location"],
        "occurred_at": _iso(row["occurred_at"]),
    }
    if legacy_embedding is not None:
        embedding_blob = cipher.encrypt_bytes(
            pack_vector(legacy_embedding), aad=f"chunks:embedding_enc:{row['id']}"
        )
        await conn.execute(
            "UPDATE memory_chunks SET meta_enc = $1, embed_model_token = $2, dims = $3,"
            f" {KEY_VERSION} = $4, embedding_enc = $5, embedding = NULL, norm = NULL"
            " WHERE id = $6",
            _seal(cipher, meta, "memory_chunks", "meta_enc", row["id"]),
            cipher.blind_token("embed_model", row["embed_model"]),
            dims,
            cipher.key_id,
            embedding_blob,
            row["id"],
        )
        return True
    await conn.execute(
        "UPDATE memory_chunks SET meta_enc = $1, embed_model_token = $2, dims = $3,"
        f" {KEY_VERSION} = $4 WHERE id = $5",
        _seal(cipher, meta, "memory_chunks", "meta_enc", row["id"]),
        cipher.blind_token("embed_model", row["embed_model"]),
        dims,
        cipher.key_id,
        row["id"],
    )
    return True


async def _seal_proactive(conn: asyncpg.Connection, cipher: Cipher, row: asyncpg.Record) -> bool:
    await conn.execute(
        "UPDATE proactive_tasks SET label_enc = $1, meta_enc = $2,"
        f" opportunity_token = $3, day_token = $4, {KEY_VERSION} = $5 WHERE id = $6",
        _seal_text(cipher, row["label"], "proactive_tasks", "label_enc", row["id"]),
        _seal(
            cipher,
            {"skill": row["skill"], "opportunity_key": row["opportunity_key"], "day": row["day"]},
            "proactive_tasks",
            "meta_enc",
            row["id"],
        ),
        cipher.blind_token("opportunity", f"{row['skill']}\x1f{row['opportunity_key']}"),
        cipher.blind_token("day", row["day"]),
        cipher.key_id,
        row["id"],
    )
    return True


async def _seal_media(conn: asyncpg.Connection, cipher: Cipher, row: asyncpg.Record) -> bool:
    meta = {
        "filename": row["filename"],
        "media_type": row["media_type"],
        "width": row["width"],
        "height": row["height"],
        "duration_s": float(row["duration_s"]) if row["duration_s"] is not None else None,
        "origin": row["origin"],
    }
    await conn.execute(
        "UPDATE media_blobs SET meta_enc = $1, origin_token = $2,"
        f" {KEY_VERSION} = $3 WHERE id = $4",
        _seal(cipher, meta, "media_blobs", "meta_enc", row["id"]),
        cipher.blind_token("source", row["origin"]),
        cipher.key_id,
        row["id"],
    )
    return True


async def _seal_provider(conn: asyncpg.Connection, cipher: Cipher, row: asyncpg.Record) -> bool:
    provider = row["provider"]
    await conn.execute(
        "UPDATE provider_connections SET account_label_enc = $1, account_id_enc = $2,"
        f" last_error_enc = $3, {KEY_VERSION} = $4 WHERE provider = $5",
        _seal_text(
            cipher, row["account_label"], "provider_connections", "account_label_enc", provider
        ),
        _seal_text(cipher, row["account_id"], "provider_connections", "account_id_enc", provider),
        _seal_text(cipher, row["last_error"], "provider_connections", "last_error_enc", provider),
        cipher.key_id,
        provider,
    )
    return True


async def _seal_sync(conn: asyncpg.Connection, cipher: Cipher, row: asyncpg.Record) -> bool:
    pk = f"{row['provider']}\x1f{row['app']}\x1f{row['key']}"
    await conn.execute(
        "UPDATE provider_sync_state SET value_enc = $1,"
        f" {KEY_VERSION} = $2 WHERE provider = $3 AND app = $4 AND key = $5",
        _seal_text(cipher, row["value"], "provider_sync_state", "value_enc", pk),
        cipher.key_id,
        row["provider"],
        row["app"],
        row["key"],
    )
    return True


async def _seal_cursor(conn: asyncpg.Connection, cipher: Cipher, row: asyncpg.Record) -> bool:
    await conn.execute(
        f"UPDATE connector_cursors SET last_seen_enc = $1, {KEY_VERSION} = $2 WHERE source = $3",
        _seal_text(cipher, row["last_seen"], "connector_cursors", "last_seen_enc", row["source"]),
        cipher.key_id,
        row["source"],
    )
    return True


# ---------------------------------------------------------------------------
# the job table
# ---------------------------------------------------------------------------

JOBS: tuple[Job, ...] = (
    Job(
        "events",
        "id, source, kind, occurred_at, salience_score, memorable, meta, payload_enc",
        "source_token IS NULL OR dedup_mac IS NULL OR meta_enc IS NULL",
        apply=_seal_event,
    ),
    Job(
        "identities",
        "id, display_name, confidence",
        "profile_enc IS NULL",
        apply=_seal_identity,
    ),
    Job(
        "identity_handles",
        "id, platform, raw_handle, normalized",
        "handle_enc IS NULL OR platform_token IS NULL OR normalized_token IS NULL",
        apply=_seal_handle,
    ),
    Job("chat_messages", "id, surface", "surface_token IS NULL", apply=_seal_chat),
    Job(
        "pending_approvals",
        "id, rules_matched, note",
        "rules_matched_enc IS NULL OR note_enc IS NULL",
        apply=_seal_approval,
    ),
    Job(
        "scheduled_actions",
        "id, label, run_at",
        "meta_enc IS NULL",
        apply=_seal_scheduled,
    ),
    Job("routines", "id, label", "label_enc IS NULL", apply=_seal_routine),
    Job("decision_traces", "id, label", "label_enc IS NULL", apply=_seal_trace),
    Job(
        "memory_chunks",
        "id, source, kind, filename, location, occurred_at, embed_model, embedding, embedding_enc",
        "meta_enc IS NULL OR embed_model_token IS NULL",
        apply=_seal_chunk,
    ),
    Job(
        "proactive_tasks",
        "id, skill, opportunity_key, day, label",
        "label_enc IS NULL OR opportunity_token IS NULL OR day_token IS NULL",
        apply=_seal_proactive,
    ),
    Job(
        "media_blobs",
        "id, filename, media_type, width, height, duration_s, origin",
        "meta_enc IS NULL OR origin_token IS NULL",
        apply=_seal_media,
    ),
    Job(
        "provider_connections",
        "provider, account_label, account_id, last_error",
        "account_label_enc IS NULL OR account_id_enc IS NULL OR last_error_enc IS NULL",
        order_by="provider",
        apply=_seal_provider,
    ),
    Job(
        "provider_sync_state",
        "provider, app, key, value",
        "value_enc IS NULL",
        order_by="provider, app, key",
        apply=_seal_sync,
    ),
    Job(
        "connector_cursors",
        "source, last_seen",
        "last_seen_enc IS NULL",
        order_by="source",
        apply=_seal_cursor,
    ),
)

JOB_BY_TABLE = {job.table: job for job in JOBS}


# ---------------------------------------------------------------------------
# running it
# ---------------------------------------------------------------------------


async def pending_counts(pool: asyncpg.Pool, tables: set[str] | None = None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for job in JOBS:
        if tables is not None and job.table not in tables:
            continue
        counts[job.table] = int(await pool.fetchval(job.count_sql) or 0)
    return counts


async def backfill(
    pool: asyncpg.Pool,
    cipher: Cipher,
    *,
    batch: int = 100,
    sleep: float = 0.0,
    tables: set[str] | None = None,
) -> list[TableResult]:
    """Seal pending rows, one table at a time. A row that fails is left
    untouched and counted; rerunning retries it. A batch that seals nothing
    ends that table's pass so a permanently bad row cannot spin forever."""
    results: list[TableResult] = []
    for job in JOBS:
        if tables is not None and job.table not in tables:
            continue
        result = TableResult(job.table)
        while True:
            rows = await pool.fetch(job.select_sql, batch)
            if not rows:
                break
            sealed_here = 0
            async with pool.acquire() as conn:
                for row in rows:
                    try:
                        async with conn.transaction():
                            applied = await job.apply(conn, cipher, row)
                    except Exception:
                        result.failed += 1
                        log.warning(
                            "sealing one %s row failed — left for the next run",
                            job.table,
                            exc_info=True,
                        )
                        continue
                    if applied:
                        sealed_here += 1
                    else:
                        result.failed += 1
            result.sealed += sealed_here
            if sealed_here == 0:
                break
            if sleep:
                await asyncio.sleep(sleep)
        result.remaining = int(await pool.fetchval(job.count_sql) or 0)
        results.append(result)
        log.info(
            "seal %s: %d sealed, %d failed, %d still pending",
            result.table,
            result.sealed,
            result.failed,
            result.remaining,
        )
    return results


async def seal_for_cutover(
    pool: asyncpg.Pool, cipher: Cipher, *, batch: int = 100, sleep: float = 0.0
) -> list[TableResult]:
    """The automatic deploy step: seal every legacy row before `018` drops
    the plaintext columns. Raises rather than letting the cutover run over
    unsealed content, so a bad key stops the boot instead of erasing data."""
    results = await backfill(pool, cipher, batch=batch, sleep=sleep)
    remaining = sum(result.remaining for result in results)
    failed = sum(result.failed for result in results)
    if remaining or failed:
        raise RuntimeError(
            "sealed storage cutover refused: "
            f"{remaining} row(s) still pending, {failed} could not be sealed — "
            "run `python -m aether.memory.seal_migrate --apply` with the right key"
        )
    sealed = sum(result.sealed for result in results)
    log.info("sealed storage cutover: %d row(s) sealed across %d table(s)", sealed, len(results))
    return results


async def legacy_columns_present(pool: asyncpg.Pool) -> bool:
    """True while any table in the migration ledger still has a plaintext
    column — the CLI's cue that the cutover has already happened."""
    rows = await pool.fetch(
        "SELECT table_name, column_name FROM information_schema.columns"
        " WHERE table_schema = 'public'"
    )
    for record in rows:
        table = str(record["table_name"])
        if str(record["column_name"]) in LEGACY_COLUMNS.get(table, frozenset()):
            return True
    return False


def _cipher(settings: Settings) -> Cipher:
    if not settings.encryption_key:
        raise SystemExit(
            "AETHER_ENCRYPTION_KEY is not set — the sealer needs the same key the app uses"
        )
    try:
        return Cipher.from_b64(
            settings.encryption_key,
            key_id=settings.encryption_key_id or "k1",
            previous_b64=settings.encryption_key_previous,
            previous_id=settings.encryption_key_previous_id,
        )
    except CryptoError as exc:
        raise SystemExit(f"encryption key is invalid: {exc}") from exc


async def _main(args: argparse.Namespace) -> int:
    settings = Settings()
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is not set")
    cipher = _cipher(settings)
    tables = {args.table} if args.table else None
    if tables is not None and not tables <= set(JOB_BY_TABLE):
        raise SystemExit(f"unknown table {args.table!r}; choose from {', '.join(JOB_BY_TABLE)}")
    pool = await create_pool(settings.database_url)
    try:
        if not await legacy_columns_present(pool):
            print("the sealed cutover is already applied — nothing left to seal")
            return 0
        if args.verify_only:
            counts = await pending_counts(pool, tables)
            for table, count in counts.items():
                print(f"{table}: {count} row(s) still pending")
            return 0 if all(count == 0 for count in counts.values()) else 1
        if not args.apply:
            counts = await pending_counts(pool, tables)
            for table, count in counts.items():
                print(f"{table}: {count} row(s) pending")
            print("dry run — add --apply to seal")
            return 0
        results = await backfill(pool, cipher, batch=args.batch, sleep=args.sleep, tables=tables)
        for result in results:
            print(
                f"{result.table}: {result.sealed} sealed, "
                f"{result.failed} failed, {result.remaining} pending"
            )
        return 0 if all(result.remaining == 0 for result in results) else 1
    finally:
        await pool.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the sealed columns")
    parser.add_argument("--verify-only", action="store_true", help="report pending rows")
    parser.add_argument("--table", default="", help="one table from the job list")
    parser.add_argument("--batch", type=int, default=100)
    parser.add_argument(
        "--sleep", type=float, default=0.05, help="pause between batches, in seconds"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
