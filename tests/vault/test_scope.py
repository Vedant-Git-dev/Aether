from __future__ import annotations

import pytest
from fake_vault import FakeVault

from aether.memory.crypto import Cipher, generate_key_b64
from aether.memory.storage import ScopedStore
from aether.vault.errors import VaultProtocolError
from aether.vault.scope import (
    KeyScope,
    LegacyCiphertextError,
    Location,
    VaultClosed,
    VaultExhausted,
    VaultLocked,
)

VAULT_ID = bytes(range(16))
TASK = bytes(range(16, 32))
OTHER_TASK = bytes(range(32, 48))


def location(**overrides) -> Location:
    base = {
        "cat": "memory",
        "ver": 1,
        "table": "events",
        "column": "payload_enc",
        "row": "42",
        "purpose": "content",
    }
    base.update(overrides)
    return Location(**base)


async def test_round_trip_and_handle_cleanup() -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        blob = scope.seal(location(), b"hello")
        assert blob.startswith(b"AE2\x00")
        assert scope.open(location(), blob) == b"hello"
        assert scope.refs == (("memory", 1),)
    assert vault.acquired == 1
    assert vault.released == 1
    assert scope.closed
    assert all(handle.zeroed for handle in vault.created_handles)


async def test_location_binds_row_column_purpose_category_and_version() -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        blob = scope.seal(location(), b"secret")
        for changed in (
            location(row="43"),
            location(table="other"),
            location(column="meta_enc"),
            location(purpose="other"),
            location(cat="workspace"),
            location(ver=2),
        ):
            with pytest.raises((VaultProtocolError, VaultLocked)):
                scope.open(changed, blob)


async def test_task_without_the_grant_is_refused() -> None:
    vault = FakeVault(
        vault_id=VAULT_ID,
        allow=lambda task, operation, refs: task != OTHER_TASK,
    )
    with pytest.raises(VaultLocked):
        async with vault.acquire(OTHER_TASK, "turn", [("memory", 1)]):
            raise AssertionError("the scope should not open")
    assert vault.created_handles == []


async def test_locked_vault_is_refused_before_any_handle_exists() -> None:
    vault = FakeVault(vault_id=VAULT_ID, available=False)
    with pytest.raises(VaultLocked):
        async with vault.acquire(TASK, "turn", [("memory", 1)]):
            raise AssertionError("the scope should not open")
    assert vault.acquired == 0
    assert vault.created_handles == []


async def test_operation_categories_are_enforced() -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    with pytest.raises(VaultProtocolError) as caught:
        async with vault.acquire(TASK, "turn", [("credentials", 1)]):
            raise AssertionError("the scope should not open")
    assert caught.value.code == "bad_scope"
    assert vault.created_handles == []


async def test_ae1_ciphertext_is_refused() -> None:
    cipher = Cipher.from_b64(generate_key_b64())
    legacy = cipher.encrypt_text("old", aad="events:payload_enc:42")
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        with pytest.raises(LegacyCiphertextError):
            scope.open(location(), legacy)


async def test_nonce_budget_stops_sealing() -> None:
    vault = FakeVault(vault_id=VAULT_ID, max_operations=1)
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        scope.seal(location(), b"one")
        with pytest.raises(VaultExhausted):
            scope.seal(location(row="43"), b"two")


async def test_use_after_close_is_refused() -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        blob = scope.seal(location(), b"secret")
        scope.close()
        with pytest.raises(VaultClosed):
            scope.seal(location(), b"again")
        with pytest.raises(VaultClosed):
            scope.open(location(), blob)
        with pytest.raises(VaultClosed):
            scope.token("event.source", "x")
        with pytest.raises(VaultClosed):
            scope.dedup("event.dedup", b"x")


async def test_tokens_are_domain_separated_and_stable() -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        first = scope.token("event.source", "telegram")
        assert first == scope.token("event.source", "telegram")
        assert first != scope.token("event.source", "discord")
        assert first != scope.token("chat.surface", "telegram")
        with pytest.raises(VaultProtocolError):
            scope.token("not.a.domain", "x")


async def test_token_needs_its_category_grant() -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "index", [("memory", 1)]) as scope:
        assert scope.token("event.source", "telegram")
        with pytest.raises(VaultLocked):
            scope.token("alert.scope", "telegram")


async def test_dedup_is_domain_separated_and_needs_its_grant() -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        assert scope.dedup("event.dedup", b"same") == scope.dedup("event.dedup", b"same")
        assert scope.dedup("event.dedup", b"same") != scope.dedup("event.dedup", b"other")
        with pytest.raises(VaultLocked):
            scope.dedup("media.content", b"same")


async def test_close_retires_every_handle_even_when_one_fails() -> None:
    class Handle:
        def __init__(self, cat: str, broken: bool = False) -> None:
            self.cat = cat
            self.ver = 1
            self.broken = broken
            self.closed = False

        def close(self) -> None:
            if self.broken:
                raise RuntimeError("close failed")
            self.closed = True

    broken = Handle("memory", broken=True)
    healthy = Handle("workspace")
    scope = KeyScope(
        vault_id=VAULT_ID,
        task=TASK,
        operation="turn",
        handles={("memory", 1): broken, ("workspace", 1): healthy},
    )
    with pytest.raises(RuntimeError):
        scope.close()
    assert scope.closed
    assert healthy.closed
    scope.close()  # idempotent, and does not re-raise


async def test_scoped_store_round_trip_and_location_binding() -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        store = ScopedStore(scope, cat="memory", ver=1)
        blob = store.seal({"text": "hello"}, table="events", column="payload_enc", row_id=7)
        assert store.unseal(blob, table="events", column="payload_enc", row_id=7) == {
            "text": "hello"
        }
        with pytest.raises(VaultProtocolError):
            store.unseal(blob, table="events", column="payload_enc", row_id=8)
        raw = store.seal_bytes(b"\x00\x01", table="events", column="meta_enc", row_id=7)
        assert store.unseal_bytes(raw, table="events", column="meta_enc", row_id=7) == b"\x00\x01"


async def test_refusal_with_a_missing_key_constructs_no_handles() -> None:
    vault = FakeVault(vault_id=VAULT_ID)
    with pytest.raises(VaultLocked):
        async with vault.acquire(TASK, "turn", [("memory", 1), ("workspace", 2)]):
            pytest.fail("refused keys entered a scope")
    assert vault.created_handles == []


async def test_reacquiring_does_not_reset_key_budget() -> None:
    vault = FakeVault(vault_id=VAULT_ID, max_operations=1)
    async with vault.acquire(TASK, "turn", [("memory", 1)]) as scope:
        scope.seal(location(), b"one")
    async with vault.acquire(OTHER_TASK, "turn", [("memory", 1)]) as scope:
        with pytest.raises(VaultExhausted):
            scope.seal(location(), b"two")
