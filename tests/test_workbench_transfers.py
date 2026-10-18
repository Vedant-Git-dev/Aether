"""One-time transfer URLs — the single-use contract, against a real
Postgres (the integration fixture; skipped without
AETHER_TEST_DATABASE_URL). A bundle is served exactly once, a result is
accepted exactly once, and an unknown, spent, or expired URL is simply
dead — the token in the URL never touches the database, only its hash.
"""

from __future__ import annotations

import pytest

from aether.memory.crypto import Cipher
from aether.workbench.transfers import BUNDLE, RESULT, TransferError, TransferStore

pytestmark = pytest.mark.integration


async def a_store(db) -> TransferStore:
    return TransferStore(db, Cipher(b"t" * 32))


async def test_a_bundle_is_served_exactly_once(db) -> None:
    store = await a_store(db)
    ticket = await store.mint(
        kind=BUNDLE,
        payload={"script_name": "job.py", "files": {"job.py": "print(1)"}},
        ttl_seconds=600,
    )
    served = await store.take_bundle(ticket.token)
    assert served == {"script_name": "job.py", "files": {"job.py": "print(1)"}}
    # the same URL again — dead
    assert await store.take_bundle(ticket.token) is None


async def test_an_unknown_or_wrong_kind_token_is_dead(db) -> None:
    store = await a_store(db)
    assert await store.take_bundle("no-such-token") is None
    result_ticket = await store.mint(kind=RESULT, payload={}, ttl_seconds=600)
    # a result URL can't be read as a bundle
    assert await store.take_bundle(result_ticket.token) is None


async def test_an_expired_bundle_is_dead(db) -> None:
    store = await a_store(db)
    ticket = await store.mint(kind=BUNDLE, payload={"a": 1}, ttl_seconds=-10)
    assert await store.take_bundle(ticket.token) is None


async def test_a_result_is_accepted_exactly_once(db) -> None:
    store = await a_store(db)
    ticket = await store.mint(kind=RESULT, payload={}, ttl_seconds=600)
    # nothing to collect before the runner has reported
    assert await store.collect_result(ticket.token) is None
    assert await store.post_result(ticket.token, {"exit_code": 0, "stdout": "ok", "stderr": ""})
    assert await store.collect_result(ticket.token) == {
        "exit_code": 0,
        "stdout": "ok",
        "stderr": "",
    }
    # a second post onto the same URL — refused, the first answer stands
    assert not await store.post_result(
        ticket.token, {"exit_code": 1, "stdout": "", "stderr": "evil"}
    )
    assert await store.collect_result(ticket.token) == {
        "exit_code": 0,
        "stdout": "ok",
        "stderr": "",
    }


async def test_a_bundle_token_cannot_be_posted_to_as_a_result(db) -> None:
    store = await a_store(db)
    ticket = await store.mint(kind=BUNDLE, payload={"a": 1}, ttl_seconds=600)
    assert not await store.post_result(ticket.token, {"exit_code": 0})


async def test_the_kind_vocabulary_is_closed(db) -> None:
    store = await a_store(db)
    with pytest.raises(TransferError):
        await store.mint(kind="evil", payload={}, ttl_seconds=60)


async def test_tokens_never_touch_the_table_only_their_hashes(db) -> None:
    store = await a_store(db)
    ticket = await store.mint(kind=BUNDLE, payload={"a": 1}, ttl_seconds=600)
    row = await db.fetchrow("SELECT token_hash, payload_enc FROM workbench_transfers")
    assert ticket.token.encode() not in row["payload_enc"]
    assert bytes(row["token_hash"]) != ticket.token.encode()
    # and the payload isn't readable text on disk
    assert b"print" not in bytes(row["payload_enc"])


async def test_expired_rows_are_pruned_best_effort(db) -> None:
    store = await a_store(db)
    await store.mint(kind=BUNDLE, payload={"a": 1}, ttl_seconds=-10)
    await store.mint(kind=BUNDLE, payload={"a": 2}, ttl_seconds=600)
    await store.drop_expired()
    remaining = await db.fetchval("SELECT count(*) FROM workbench_transfers")
    assert remaining == 1
