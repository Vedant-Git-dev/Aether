"""The encrypted media store.

Unit half (no DB): magic-byte sniffing is the only source of type
truth — a declared MIME never wins — and the store refuses empty
bytes before touching anything.

Integration half (real Postgres, the scratch-DB suite): the round
trip, keyed dedup and its ON CONFLICT race, least-recently-use
eviction, the status-filtered protected set (a resolved-but-uncleared
or lazily expired reference must stop protecting on its own), and the
refuse-rather-than-breach quota. Review asked for these against a
real database — the eviction SQL (window sums, xmax, partial
protection) is exactly the kind of thing a fake pool would faithfully
agree with and faithfully lie about.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from aether.config import MediaConfig
from aether.media.store import MediaStore, MediaStoreFull, sniff_media_type
from aether.memory.crypto import Cipher, generate_key_b64

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def _cipher() -> Cipher:
    return Cipher.from_b64(generate_key_b64())


def _store(pool, config: MediaConfig | None = None) -> MediaStore:
    return MediaStore(pool, _cipher(), config or MediaConfig())


# -- unit: type truth is sniffed, never declared -------------------------------


def test_sniffing_knows_the_image_signatures() -> None:
    assert sniff_media_type(PNG) == "image/png"
    assert sniff_media_type(b"\xff\xd8\xff" + b"0" * 16) == "image/jpeg"
    assert sniff_media_type(b"GIF89a" + b"0" * 16) == "image/gif"
    assert sniff_media_type(b"RIFF" + b"0" * 4 + b"WEBP") == "image/webp"


def test_sniffing_knows_the_media_signatures() -> None:
    assert sniff_media_type(b"0" * 4 + b"ftypisom" + b"0" * 8) == "video/mp4"
    assert sniff_media_type(b"\x1a\x45\xdf\xa3" + b"0" * 16) == "video/webm"
    assert sniff_media_type(b"OggS" + b"0" * 16) == "audio/ogg"
    assert sniff_media_type(b"ID3" + b"0" * 16) == "audio/mpeg"
    assert sniff_media_type(b"%PDF-1.7" + b"0" * 16) == "application/pdf"
    assert sniff_media_type(b"PK" + b"0" * 16) == "application/zip"


def test_sniffing_refuses_to_trust_a_declared_type() -> None:
    # an HTML or SVG body arriving as an "image" is exactly the stored-XSS
    # shape — it sniffs as unknown and can only ever serve as a download
    html = b"<!DOCTYPE html><script>alert(1)</script>"
    assert sniff_media_type(html) == "application/octet-stream"
    assert sniff_media_type(b"<svg xmlns=" + b"0" * 8) == "application/octet-stream"


async def test_empty_bytes_are_refused_before_anything_else() -> None:
    store = MediaStore(None, _cipher(), MediaConfig())  # the pool is never reached
    with pytest.raises(ValueError):
        await store.put(b"")


# -- integration: the scratch-DB suite -----------------------------------------


@pytest.mark.integration
async def test_put_get_round_trip(db) -> None:
    store = _store(db)
    ref = await store.put(PNG, filename="photo.png", origin="telegram")
    assert ref.media_type == "image/png"  # sniffed, whatever the caller said
    assert ref.size == len(PNG)
    record = await store.get(ref.media_id)
    assert record is not None
    assert record.data == PNG
    assert record.filename == "photo.png"
    assert record.media_type == "image/png"
    assert record.size == len(PNG)


@pytest.mark.integration
async def test_get_on_a_missing_id_reads_as_none(db) -> None:
    store = _store(db)
    assert await store.get(424242) is None


@pytest.mark.integration
async def test_same_bytes_dedup_to_one_row(db) -> None:
    store = _store(db)
    first = await store.put(PNG, filename="a.png", origin="telegram")
    second = await store.put(PNG, filename="totally-different.bin", origin="web")
    assert second.media_id == first.media_id  # one row, two callers, same id
    assert await store.total_bytes() == len(PNG)


@pytest.mark.integration
async def test_concurrent_same_bytes_puts_race_to_one_row(db) -> None:
    store = _store(db)
    refs = await asyncio.gather(
        *(store.put(PNG, filename=f"n{i}.png", origin="web") for i in range(5))
    )
    assert len({r.media_id for r in refs}) == 1  # the conflict path wins every race
    row = await db.fetchrow("SELECT COUNT(*) AS n FROM media_blobs")
    assert int(row["n"]) == 1


@pytest.mark.integration
async def test_eviction_takes_the_least_recently_used_first(db) -> None:
    store = _store(db, MediaConfig(storage_quota_mb=1))
    older = await store.put(b"\x89PNG" + b"a" * 400_000, filename="old.png")
    newer = await store.put(b"\x89PNG" + b"b" * 400_000, filename="new.png")
    assert await store.get(older.media_id) is not None  # bump the old one's use
    # the third put must evict exactly one blob — the one not recently used
    await store.put(b"\x89PNG" + b"c" * 400_000, filename="third.png")
    assert await store.get(older.media_id) is not None
    assert await store.get(newer.media_id) is None  # no longer stored, no raise


@pytest.mark.integration
async def test_a_pending_approval_protects_its_blob(db) -> None:
    store = _store(db, MediaConfig(storage_quota_mb=1))
    kept = await store.put(b"\x89PNG" + b"a" * 600_000, filename="kept.png")
    await db.execute(
        "INSERT INTO pending_approvals (tool_name, params_enc, status, expires_at, media_id)"
        " VALUES ('send_media', $1, 'pending', now() + interval '1 hour', $2)",
        b"x",
        kept.media_id,
    )
    with pytest.raises(MediaStoreFull):  # nothing unreferenced left to evict
        await store.put(b"\x89PNG" + b"b" * 600_000, filename="next.png")
    assert await store.get(kept.media_id) is not None  # survived the refusal


@pytest.mark.integration
async def test_a_resolved_approval_stops_protecting_even_uncleared(db) -> None:
    store = _store(db, MediaConfig(storage_quota_mb=1))
    kept = await store.put(b"\x89PNG" + b"a" * 600_000, filename="kept.png")
    await db.execute(
        "INSERT INTO pending_approvals (tool_name, params_enc, status, expires_at, media_id)"
        " VALUES ('send_media', $1, 'approved', now() + interval '1 hour', $2)",
        b"x",
        kept.media_id,  # status moved on, the column deliberately never cleared
    )
    # the quota is breached and the resolved reference is NOT protection —
    # the status filter, not a cleared column, decides
    await store.put(b"\x89PNG" + b"b" * 600_000, filename="next.png")
    assert await store.get(kept.media_id) is None


@pytest.mark.integration
async def test_an_expired_approval_stops_protecting(db) -> None:
    store = _store(db, MediaConfig(storage_quota_mb=1))
    stale = await store.put(b"\x89PNG" + b"a" * 600_000, filename="stale.png")
    await db.execute(
        "INSERT INTO pending_approvals (tool_name, params_enc, status, expires_at, media_id)"
        " VALUES ('send_media', $1, 'pending', now() - interval '1 hour', $2)",
        b"x",
        stale.media_id,  # pending forever on paper — the deadline already passed
    )
    await store.put(b"\x89PNG" + b"b" * 600_000, filename="next.png")
    assert await store.get(stale.media_id) is None


@pytest.mark.integration
async def test_an_undelivered_result_protects_until_verdicted(db) -> None:
    store = _store(db, MediaConfig(storage_quota_mb=1))
    kept = await store.put(b"\x89PNG" + b"a" * 600_000, filename="kept.png")
    await db.execute(
        "INSERT INTO proactive_tasks (skill, label, opportunity_key, opportunity_enc,"
        " day, media_id, result_enc)"
        " VALUES ('media', 'l', 'k1', $1, '2026-10-18', $2, $3)",
        b"x",
        kept.media_id,
        b"r",
    )
    with pytest.raises(MediaStoreFull):  # result stored, verdict not yet given
        await store.put(b"\x89PNG" + b"b" * 600_000, filename="next.png")
    await db.execute(
        "UPDATE proactive_tasks SET feedback = 'approved' WHERE opportunity_key = 'k1'"
    )
    await store.put(b"\x89PNG" + b"b" * 600_000, filename="next.png")  # now it fits
    assert await store.get(kept.media_id) is None


@pytest.mark.integration
async def test_two_references_protect_until_both_are_gone(db) -> None:
    store = _store(db, MediaConfig(storage_quota_mb=1))
    shared = await store.put(b"\x89PNG" + b"a" * 600_000, filename="shared.png")
    await db.execute(
        "INSERT INTO pending_approvals (tool_name, params_enc, status, expires_at, media_id)"
        " VALUES ('send_media', $1, 'pending', now() + interval '1 hour', $2)",
        b"x",
        shared.media_id,
    )
    await db.execute(
        "INSERT INTO proactive_tasks (skill, label, opportunity_key, opportunity_enc,"
        " day, media_id, result_enc)"
        " VALUES ('media', 'l', 'k2', $1, '2026-10-18', $2, $3)",
        b"x",
        shared.media_id,
        b"r",
    )
    await db.execute("UPDATE pending_approvals SET status = 'denied'")  # one gone
    with pytest.raises(MediaStoreFull):  # the task still holds the reference
        await store.put(b"\x89PNG" + b"b" * 600_000, filename="next.png")
    await db.execute("UPDATE proactive_tasks SET feedback = 'discarded'")
    await store.put(b"\x89PNG" + b"b" * 600_000, filename="next.png")  # both gone


@pytest.mark.integration
async def test_stored_bytes_are_not_readable_text(db) -> None:
    store = _store(db)
    secret = b"top secret media bytes" * 10
    ref = await store.put(secret, filename="s.png")
    row = await db.fetchrow("SELECT bytes_enc FROM media_blobs WHERE id = $1", ref.media_id)
    assert secret not in row["bytes_enc"]  # the blob is sealed, not just sitting there
    mac_row = await db.fetchrow("SELECT dedup_mac FROM media_blobs WHERE id = $1", ref.media_id)
    assert mac_row["dedup_mac"] != hashlib.sha256(secret).digest()  # not a bare hash


@pytest.mark.integration
async def test_an_undecryptable_row_reads_as_no_longer_stored(db) -> None:
    pool = db
    store = MediaStore(pool, _cipher(), MediaConfig())
    ref = await store.put(PNG, filename="p.png")
    stranger = MediaStore(pool, _cipher(), MediaConfig())  # a different key entirely
    assert await stranger.get(ref.media_id) is None  # degrades, never raises
    assert await store.get(ref.media_id) is not None  # the right key still opens it


@pytest.mark.integration
async def test_evicted_media_reads_as_no_longer_stored(db) -> None:
    store = _store(db, MediaConfig(storage_quota_mb=1))
    evicted = await store.put(b"\x89PNG" + b"a" * 600_000, filename="a.png")
    await store.put(b"\x89PNG" + b"b" * 600_000, filename="b.png")  # evicts the first
    assert await store.get(evicted.media_id) is None  # the plain-note path, no raise


@pytest.mark.integration
async def test_a_put_larger_than_the_whole_quota_is_refused_whole(db) -> None:
    store = _store(db, MediaConfig(storage_quota_mb=1))
    with pytest.raises(MediaStoreFull):  # nothing evictable — the blob is the overflow
        await store.put(b"\x89PNG" + b"a" * 1_500_000, filename="huge.png")
    assert await store.total_bytes() == 0  # a refused put stores nothing at all
