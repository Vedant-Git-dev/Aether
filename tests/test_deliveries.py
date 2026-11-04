"""The delivery record: every outbound send attempt, sealed.

Plan §8. An empty message is never sent or stored; a failure is a durable
fact the next turn can be told, not a swallowed log line. These run against
a real database to prove the status vocabulary and ciphertext-at-rest.
"""

from __future__ import annotations

import pytest

from aether.memory.crypto import Cipher, generate_key_b64
from aether.memory.deliveries import ABANDONED, FAILED, QUEUED, SENT, Deliveries

SECRET = "AETHER-DELIVERY-CANARY-31f0"


def _store(db) -> Deliveries:
    return Deliveries(db, Cipher.from_b64(generate_key_b64()))


@pytest.mark.integration
async def test_an_empty_message_is_refused_not_stored(db) -> None:
    store = _store(db)

    assert await store.record_queued("telegram", "") is None
    assert await store.record_queued("telegram", "   ") is None
    assert await db.fetchval("SELECT COUNT(*) FROM deliveries") == 0


@pytest.mark.integration
async def test_a_sent_delivery_round_trips_with_its_message_id(db) -> None:
    store = _store(db)
    record = await store.record_queued("telegram", "going out")
    assert record is not None
    assert record.status == QUEUED

    await store.mark_sent(record.id, "platform-42")

    found = await store.get(record.id)
    assert found is not None
    assert found.status == SENT
    assert found.body == "going out"
    assert found.surface == "telegram"
    assert found.message_id == "platform-42"


@pytest.mark.integration
async def test_a_failure_is_recorded_and_listed_as_undelivered(db) -> None:
    store = _store(db)
    record = await store.record_queued("discord", "never arrived")
    assert record is not None

    await store.mark_failed(record.id, "socket closed")

    undelivered = await store.undelivered()
    assert [item.id for item in undelivered] == [record.id]
    assert undelivered[0].status == FAILED
    assert undelivered[0].error == "socket closed"


@pytest.mark.integration
async def test_an_abandoned_delivery_is_not_retried(db) -> None:
    store = _store(db)
    record = await store.record_queued("telegram", "still not delivered")
    assert record is not None
    await store.mark_failed(record.id, "first failure")

    await store.mark_abandoned(record.id, "re-delivery failed")

    found = await store.get(record.id)
    assert found is not None and found.status == ABANDONED
    assert await store.undelivered() == []


@pytest.mark.integration
async def test_the_body_and_error_are_ciphertext_at_rest(db) -> None:
    store = _store(db)
    record = await store.record_queued("telegram", SECRET)
    assert record is not None
    await store.mark_failed(record.id, f"carrying {SECRET}")

    row = await db.fetchrow("SELECT body_enc, error_enc FROM deliveries WHERE id = $1", record.id)
    assert SECRET.encode() not in row["body_enc"]
    assert SECRET.encode() not in row["error_enc"]


# ---------------------------------------------------------------------------
# the fanout's side of the rule (plan §8) — a fake recorder, no database
# ---------------------------------------------------------------------------


class _MemoryDeliveries:
    """The delivery store's shape, in memory, for the fanout's unit tests."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def record_queued(self, surface: str, text: str):
        from types import SimpleNamespace

        if not text.strip():
            return None
        row = {
            "id": len(self.rows) + 1,
            "surface": surface,
            "body": text,
            "status": "queued",
            "message_id": "",
            "error": "",
        }
        self.rows.append(row)
        return SimpleNamespace(**row)

    async def mark_sent(self, delivery_id: int, message_id: str = "") -> None:
        self.rows[delivery_id - 1].update(status="sent", message_id=message_id)

    async def mark_failed(self, delivery_id: int, error: str) -> None:
        self.rows[delivery_id - 1].update(status="failed", error=error)

    async def mark_abandoned(self, delivery_id: int, error: str = "") -> None:
        self.rows[delivery_id - 1].update(status="abandoned", error=error)


class _Connector:
    name = "telegram"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[str] = []

    async def send_to_user(self, text: str) -> str:
        if self.fail:
            raise RuntimeError("socket closed")
        self.sent.append(text)
        return "m1"


class _Transcript:
    def __init__(self) -> None:
        self.appended: list[tuple[str, str, str]] = []

    async def append(self, surface: str, direction: str, text: str) -> None:
        self.appended.append((surface, direction, text))


async def test_an_empty_message_is_never_sent_or_stored() -> None:
    from aether.agent.loop import SurfaceFanout

    connector = _Connector()
    transcript = _Transcript()
    deliveries = _MemoryDeliveries()

    refs = await SurfaceFanout([connector], history=transcript, deliveries=deliveries).send_to_user(
        "   "
    )

    assert refs == {}
    assert connector.sent == []
    assert transcript.appended == []
    assert deliveries.rows == []


async def test_a_failure_is_recorded_and_not_rendered_as_assistant_text() -> None:
    from aether.agent.loop import SurfaceFanout

    connector = _Connector(fail=True)
    transcript = _Transcript()
    deliveries = _MemoryDeliveries()

    refs = await SurfaceFanout([connector], history=transcript, deliveries=deliveries).send_to_user(
        "never arrived"
    )

    assert refs == {}
    assert transcript.appended == []  # a failed send is not the assistant's words
    assert [row["status"] for row in deliveries.rows] == ["failed"]
    assert deliveries.rows[0]["error"] == "socket closed"


async def test_a_successful_send_is_recorded_as_sent() -> None:
    from aether.agent.loop import SurfaceFanout

    deliveries = _MemoryDeliveries()

    refs = await SurfaceFanout([_Connector()], deliveries=deliveries).send_to_user("going out")

    assert refs == {"telegram": "m1"}
    assert deliveries.rows[0]["status"] == "sent"
    assert deliveries.rows[0]["message_id"] == "m1"
