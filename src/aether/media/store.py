"""Encrypted media storage — the keep-what-was-shared half of media.

Images, voice notes, videos, and generated files all become one
AES-256-GCM blob per row, bound to the row id the same way event
payloads are (memory/events.py). Plain columns are metadata only.

The stored type is sniffed from magic bytes at put time; whatever the
sending platform claimed the file was is recorded as filename metadata
and never trusted for serving or classification.

Dedup is content-addressed but keyed: the stored tag is an HMAC over
an HKDF subkey of the encryption secret (crypto.Cipher.dedup_mac), so a
database reader cannot use the tag to confirm that some known file is
stored here, the way a bare sha256 column would let them.

Eviction is least-recently-used — put (including dedup hits) and get
both bump last_used_at — and skips anything a pending approval or an
undelivered task result still references. The protected set comes from
the reference columns filtered by status, so a missed clear or a lazy
expiry can never strand a blob or starve the quota. A put that would
breach the quota with nothing evictable raises MediaStoreFull; callers
turn that into a plain note, never a crash.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..config import MediaConfig
from ..memory.crypto import Cipher, CryptoError

log = logging.getLogger("aether.media")

AAD_COLUMN = "bytes_enc"

# magic-byte signatures — the only source of truth for what a blob is
_PNG = b"\x89PNG\r\n\x1a\n"
_WEBP = b"RIFF"


def sniff_media_type(data: bytes) -> str:
    """The MIME the bytes themselves declare, by signature. Anything
    unrecognized is application/octet-stream — the serving allowlist
    treats that as a download, never an inline render."""
    if data[:8] == _PNG:
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == _WEBP and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp":
        return "video/mp4"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm"
    if data[:4] == b"OggS":
        return "audio/ogg"
    if data[:3] == b"ID3" or (len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    if data[:5] == b"%PDF-":
        return "application/pdf"
    if data[:2] == b"PK":
        return "application/zip"
    return "application/octet-stream"


@dataclass(frozen=True)
class MediaRef:
    """What a put hands back — the id and the sniffed truth, small
    enough to ride an event payload or a result row."""

    media_id: int
    media_type: str
    filename: str
    size: int


@dataclass(frozen=True)
class MediaRecord:
    """A stored blob opened: metadata plus the decrypted bytes."""

    media_id: int
    filename: str
    media_type: str
    size: int
    data: bytes


@dataclass(frozen=True)
class MediaSummary:
    """One stored blob's outside — metadata only, the bytes stay sealed.
    What a resolver needs to match a file by fact: id, sniffed kind,
    stored name, size, where it came from, and when."""

    media_id: int
    filename: str
    media_type: str
    size: int
    origin: str
    created_at: datetime


class MediaStoreFull(Exception):
    """The quota is spent and nothing evictable remains. The caller
    turns this into a plain note — media is refused, never a crash."""


# the blobs eviction must never touch: referenced by a still-pending
# approval (expiry included in the filter — a lazily expired row stops
# protecting on its own) or by an undelivered task result (a result
# stored but not yet given a verdict). Status, not a cleared column,
# decides — a missed clear cannot strand a blob.
_PROTECTED_SQL = """
    SELECT media_id FROM pending_approvals
     WHERE media_id IS NOT NULL
       AND status = 'pending'
       AND expires_at > now()
    UNION
    SELECT media_id FROM proactive_tasks
     WHERE media_id IS NOT NULL
       AND result_enc IS NOT NULL
       AND feedback IS NULL
"""


class MediaStore:
    """Encrypted blobs in Postgres, kept under a live-read quota."""

    def __init__(self, pool: Any, cipher: Cipher, config: MediaConfig) -> None:
        self._pool = pool
        self._cipher = cipher
        self._config = config

    @property
    def quota_bytes(self) -> int:
        return self._config.storage_quota_mb * 1024 * 1024

    async def put(
        self,
        data: bytes,
        *,
        filename: str = "",
        origin: str = "",
        width: int | None = None,
        height: int | None = None,
        duration_s: float | None = None,
    ) -> MediaRef:
        """Store bytes (or return the existing row for the same bytes).
        Raises MediaStoreFull when the quota is spent with nothing
        evictable left."""
        if not data:
            raise ValueError("the media store refuses empty bytes")
        mac = self._cipher.dedup_mac(data)
        media_type = sniff_media_type(data)
        async with self._pool.acquire() as conn, conn.transaction():
            # one statement settles the race: the first same-bytes put
            # inserts, every later one lands on the conflict path and
            # walks away with the existing id (and a fresh last_used_at)
            row = await conn.fetchrow(
                "INSERT INTO media_blobs"
                " (filename, media_type, size, width, height, duration_s,"
                "  origin, dedup_mac, bytes_enc)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)"
                " ON CONFLICT (dedup_mac)"
                " DO UPDATE SET last_used_at = now()"
                " RETURNING id, media_type, filename, size, (xmax = 0) AS inserted",
                filename,
                media_type,
                len(data),
                width,
                height,
                duration_s,
                origin,
                mac,
                b"",
            )
            media_id = int(row["id"])
            if not row["inserted"]:  # dedup hit — the bytes already exist
                return MediaRef(
                    media_id=media_id,
                    media_type=str(row["media_type"]),
                    filename=str(row["filename"]),
                    size=int(row["size"]),
                )
            # the new row is already in the table — make_room counts it in
            # the total, must never count it as eviction fodder, and on
            # refusal the surrounding transaction rolls the insert back
            await self._make_room(conn, media_id)
            blob = self._cipher.encrypt_bytes(data, aad=f"media:{AAD_COLUMN}:{media_id}")
            await conn.execute(
                "UPDATE media_blobs SET bytes_enc = $1 WHERE id = $2",
                blob,
                media_id,
            )
        return MediaRef(media_id=media_id, media_type=media_type, filename=filename, size=len(data))

    async def get(self, media_id: int) -> MediaRecord | None:
        """One blob opened, its last use refreshed. None means the caller
        should say the plain words — gone, evicted, or unreadable all
        read the same from the outside and never raise."""
        row = await self._pool.fetchrow(
            "SELECT filename, media_type, size, bytes_enc FROM media_blobs WHERE id = $1",
            media_id,
        )
        if row is None:
            return None
        try:
            data = self._cipher.decrypt_bytes(
                row["bytes_enc"], aad=f"media:{AAD_COLUMN}:{media_id}"
            )
        except CryptoError:
            log.warning("media %s is undecryptable — reading as no longer stored", media_id)
            return None
        await self._pool.execute(
            "UPDATE media_blobs SET last_used_at = now() WHERE id = $1",
            media_id,
        )
        return MediaRecord(
            media_id=media_id,
            filename=str(row["filename"]),
            media_type=str(row["media_type"]),
            size=int(row["size"]),
            data=data,
        )

    async def recent(self, limit: int = 8) -> list[MediaSummary]:
        """The newest stored blobs, metadata only — bytes stay sealed and
        no last-use is bumped, so listing never reshuffles eviction. The
        resolver's eyes: which files exist, what each sniffed as, and
        how new it is, so a "the video I sent" resolves by fact and
        never by a guessed id."""
        rows = await self._pool.fetch(
            "SELECT id, filename, media_type, size, origin, created_at"
            " FROM media_blobs ORDER BY created_at DESC, id DESC LIMIT $1",
            limit,
        )
        return [
            MediaSummary(
                media_id=int(row["id"]),
                filename=str(row["filename"]),
                media_type=str(row["media_type"]),
                size=int(row["size"]),
                origin=str(row["origin"] or ""),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def total_bytes(self) -> int:
        """Everything currently kept — the quota's other side."""
        row = await self._pool.fetchrow("SELECT COALESCE(SUM(size), 0) AS total FROM media_blobs")
        return int(row["total"]) if row else 0

    async def _make_room(self, conn: Any, media_id: int) -> None:
        """Evict least-recently-used unprotected blobs until the table
        fits the quota again. The just-inserted row is already counted
        in the total and is never eviction fodder; when nothing else
        can cover the need, MediaStoreFull aborts the put (and the
        surrounding transaction rolls the insert back — a refused put
        stores nothing)."""
        row = await conn.fetchrow("SELECT COALESCE(SUM(size), 0) AS total FROM media_blobs")
        total = int(row["total"])
        if total <= self.quota_bytes:
            return
        need = total - self.quota_bytes
        evictable = await conn.fetchrow(
            "SELECT COALESCE(SUM(size), 0) AS freeable FROM media_blobs"
            " WHERE id <> $1"
            f" AND id NOT IN ({_PROTECTED_SQL})",
            media_id,
        )
        if int(evictable["freeable"]) < need:
            raise MediaStoreFull("media storage is full — nothing unreferenced is left to remove")
        # walking the LRU order, every row whose predecessors have not
        # yet covered the need goes; the row that crosses the line is
        # the one that completes the eviction, taken whole
        await conn.execute(
            "WITH doomed AS ("
            "  SELECT id FROM ("
            "    SELECT id, size,"
            "           SUM(size) OVER (ORDER BY last_used_at ASC, id ASC) AS running"
            "    FROM media_blobs WHERE id <> $1"
            f"    AND id NOT IN ({_PROTECTED_SQL})"
            "  ) ranked WHERE running - size < $2"
            ")"
            " DELETE FROM media_blobs WHERE id IN (SELECT id FROM doomed)",
            media_id,
            need,
        )
        log.info("evicted %d bytes of media to fit the quota", need)
