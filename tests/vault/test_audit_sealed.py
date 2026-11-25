"""The sealed audit chain: entries hashed over ciphertext, the legacy chain
still verifiable, and the format transition authenticated in place."""

from __future__ import annotations

import pytest
from fake_vault import FakeVault

from aether.authz.audit import AuditLog, ChainVerification, verify_entries
from aether.memory.crypto import Cipher, generate_key_b64
from aether.memory.rows import CipherRows
from aether.vault.schema import AE2_MAGIC
from aether.vault.scope import VaultLocked

pytestmark = pytest.mark.integration

VAULT_ID = bytes(range(16))
TASK = bytes(range(16, 32))
OPS = [("operations", 1)]
MEMORY = [("memory", 1)]

CANARY = "canary-6f3a-in-the-params"


class _NoSqlPool:
    async def fetchrow(self, *a: object, **k: object) -> None:
        raise AssertionError("a denied read issued SQL")

    async def fetch(self, *a: object, **k: object) -> None:
        raise AssertionError("a denied read issued SQL")

    async def fetchval(self, *a: object, **k: object) -> None:
        raise AssertionError("a denied read issued SQL")

    async def execute(self, *a: object, **k: object) -> None:
        raise AssertionError("a denied read issued SQL")

    def acquire(self) -> None:
        raise AssertionError("a denied read issued SQL")


def _sealed(db, cipher: Cipher) -> AuditLog:
    return AuditLog(db, CipherRows(cipher))


async def test_sealed_chain_round_trip_and_no_plaintext(db) -> None:
    audit = _sealed(db, Cipher.from_b64(generate_key_b64()))
    seq = await audit.append(
        actor="agent",
        tool_name="mail__send_message",
        decision="allow",
        rules_matched="builtin:risky",
        params={"to": "a@b.c", "body": CANARY},
        outcome="sent",
    )
    row = await db.fetchrow(
        "SELECT actor, tool_name, decision, rules_matched, params_digest, outcome,"
        " payload_enc FROM audit_log WHERE seq = $1",
        seq,
    )
    assert row["payload_enc"]
    # every human-readable field is empty in the clear
    assert row["actor"] == "" and row["tool_name"] == "" and row["outcome"] == ""
    assert row["rules_matched"] == "" and row["params_digest"] == ""
    assert CANARY.encode() not in row["payload_enc"]

    assert (await audit.verify_chain()).ok
    recent = await audit.recent(1)
    assert recent[0]["actor"] == "agent"
    assert recent[0]["tool_name"] == "mail__send_message"
    assert recent[0]["rules_matched"] == "builtin:risky"


async def test_a_parameter_guess_cannot_confirm_a_value(db) -> None:
    """The old unkeyed params digest is gone from the clear: nothing a caller
    can compute from a guessed dictionary matches a stored plaintext field."""
    audit = _sealed(db, Cipher.from_b64(generate_key_b64()))
    await audit.append(
        actor="agent", tool_name="payments__pay", decision="approve", params={"amount": 5}
    )
    stored = await db.fetch("SELECT actor, tool_name, params_digest FROM audit_log")
    assert all(r["params_digest"] == "" for r in stored)
    assert all(r["tool_name"] == "" for r in stored)


async def test_bit_flip_in_the_ciphertext_breaks_the_chain(db) -> None:
    audit = _sealed(db, Cipher.from_b64(generate_key_b64()))
    first = await audit.append(actor="agent", tool_name="a__one", decision="info")
    await audit.append(actor="agent", tool_name="a__two", decision="info")
    blob = await db.fetchval("SELECT payload_enc FROM audit_log WHERE seq = $1", first)
    broken = bytearray(blob)
    broken[-1] ^= 0xFF
    await db.execute("UPDATE audit_log SET payload_enc = $1 WHERE seq = $2", bytes(broken), first)
    verification = await audit.verify_chain()
    assert not verification.ok
    assert verification.first_bad_seq == first


async def test_reorder_and_truncation_are_visible(db) -> None:
    audit = _sealed(db, Cipher.from_b64(generate_key_b64()))
    for index in range(3):
        await audit.append(actor="agent", tool_name=f"a__{index}", decision="info")
    checkpoint = await audit.verify_chain()
    assert checkpoint.ok and checkpoint.entries == 3 and checkpoint.head_hash

    # reordering: make the last row point at the wrong predecessor
    await db.execute("UPDATE audit_log SET prev_hash = $1 WHERE seq = 3", "f" * 64)
    broken = await audit.verify_chain()
    assert not broken.ok and broken.first_bad_seq == 3

    # truncation: an independently retained count/head no longer matches
    await db.execute("DELETE FROM audit_log WHERE seq = 3")
    now = await audit.verify_chain()
    assert now.entries == 2
    assert now.head_hash != checkpoint.head_hash


async def test_the_transition_from_legacy_to_sealed_verifies_as_one_chain(db) -> None:
    cipher = Cipher.from_b64(generate_key_b64())
    legacy = AuditLog(db)
    await legacy.append(actor="agent", tool_name="old__one", decision="info")
    await legacy.append(actor="agent", tool_name="old__two", decision="info")

    sealed = _sealed(db, cipher)
    transition_seq = await sealed.append(actor="agent", tool_name="new__one", decision="info")
    await sealed.append(actor="agent", tool_name="new__two", decision="info")

    verification = await sealed.verify_chain()
    assert verification.ok and verification.entries == 4

    # the transition entry anchors the legacy head and count inside its
    # sealed payload
    transition = await db.fetchrow(
        "SELECT payload_enc FROM audit_log WHERE seq = $1", transition_seq
    )
    assert transition["payload_enc"]
    opened = await sealed.recent(4)
    assert [row["tool_name"] for row in opened] == [
        "new__two",
        "new__one",
        "old__two",
        "old__one",
    ]


async def test_scoped_audit_appends_and_opens(db) -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", OPS) as scope:
        audit = AuditLog.scoped(db, scope)
        await audit.append(actor="owner", tool_name="alert", decision="info", outcome="armed")
        row = await db.fetchrow("SELECT payload_enc FROM audit_log ORDER BY seq DESC LIMIT 1")
        assert row["payload_enc"].startswith(AE2_MAGIC)
        assert (await audit.verify_chain()).ok
        assert (await audit.recent(1))[0]["outcome"] == "armed"


async def test_scoped_audit_denied_issues_zero_sql() -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    pool = _NoSqlPool()
    async with vault.acquire(TASK, "turn", MEMORY) as scope:
        audit = AuditLog.scoped(pool, scope)
        with pytest.raises(VaultLocked):
            await audit.append(actor="owner", tool_name="alert", decision="info")
        with pytest.raises(VaultLocked):
            await audit.recent(1)


def test_verify_entries_accepts_plain_dicts() -> None:
    assert verify_entries([]) == ChainVerification(True, 0, head_hash=None)
