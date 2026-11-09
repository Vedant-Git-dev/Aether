"""Notification state is sealed: the key is a blind token, the text is
ciphertext, and the canary never appears in a column or a log line."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aether.memory.crypto import Cipher, generate_key_b64
from aether.memory.notifications import DbNotificationStates, MemoryNotificationStates

CANARY = "AETHER-CANARY-7f3a91"


class RecordingPool:
    """The smallest asyncpg-shaped pool: rows in, rows out."""

    def __init__(self) -> None:
        self.rows: list[tuple] = []

    async def execute(self, query: str, *args) -> None:
        self.rows.append(tuple(args))

    async def fetch(self, query: str, *args):
        cutoff = args[0] if args else None
        rows = []
        for key_token, body_enc, reported_at, key_version in self.rows:
            if cutoff is not None and reported_at < cutoff:
                continue
            rows.append(
                {
                    "key_token": key_token,
                    "body_enc": body_enc,
                    "reported_at": reported_at,
                    "key_version": key_version,
                }
            )
        return rows


async def test_the_key_never_lands_in_a_column_or_a_log(caplog) -> None:
    cipher = Cipher.from_b64(generate_key_b64())
    pool = RecordingPool()
    states = DbNotificationStates(pool, cipher)
    key = f"event:{CANARY}"

    with caplog.at_level("DEBUG"):
        await states.mark_reported((key,))

    assert len(pool.rows) == 1
    for value in pool.rows[0]:
        blob = value if isinstance(value, bytes) else str(value).encode("utf-8", "replace")
        assert CANARY.encode() not in blob
    assert CANARY not in caplog.text

    # the sealed body still carries the key for the owner's own state
    key_token, body_enc, _, _ = pool.rows[0]
    sealed = cipher.decrypt_json(
        body_enc, aad=f"notification_reports:body_enc:{bytes(key_token).hex()}"
    )
    assert sealed["key"] == key

    # and the store reads it back by equality, not by decrypting everything
    assert await states.recently_reported((key,), timedelta(minutes=5)) == {key}


async def test_the_memory_store_only_counts_a_report_inside_the_window() -> None:
    states = MemoryNotificationStates()
    now = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)

    await states.mark_reported(("event:1",), at=now - timedelta(minutes=30))

    assert await states.recently_reported(("event:1",), timedelta(minutes=60), now=now) == {
        "event:1"
    }
    assert await states.recently_reported(("event:1",), timedelta(minutes=10), now=now) == set()


async def test_a_report_for_a_different_key_does_not_block_this_one() -> None:
    states = MemoryNotificationStates()
    await states.mark_reported(("event:1",))

    assert await states.recently_reported(("event:2",), timedelta(hours=1)) == set()
