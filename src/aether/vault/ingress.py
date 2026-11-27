"""Receive sealed deliveries independently from the phone's lease state."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from secrets import token_bytes
from typing import Any, Literal

import asyncpg
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
from pyhpke import AEADId, CipherSuite, KDFId, KEMId, KEMKey

from ..authz.audit import AuditLog
from ..db_resilience import DatabaseUnavailable
from ..media.store import MediaStore
from ..memory.entities import Sender
from ..memory.events import EventStore, is_allowlisted
from ..memory.inbound import DbInboundStates
from .errors import VaultProtocolError
from .lease_work import LeaseWork
from .runtime import VaultRuntime, verified_manifest
from .schema import (
    DIGEST_BYTES,
    ID_BYTES,
    PROTOCOL_VERSION,
    X25519_BYTES,
    b64u_decode,
    b64u_encode,
    canonical_bytes,
    loads_strict,
)
from .scope import KeyScope
from .service import LeaseService
from .store import VaultStore

_CHUNK_BYTES = 256 * 1024
_INFO = b"aether/v1/ingress"
_SUITE = CipherSuite.new(KEMId.DHKEM_X25519_HKDF_SHA256, KDFId.HKDF_SHA256, AEADId.AES256_GCM)


@dataclass(frozen=True)
class TransportDelivery:
    """Output of a trusted adapter's transport authentication.

    The token is an opaque, stable identifier scoped to the authenticated
    transport. Source and routing identities belong inside the event.
    """

    receipt_token: bytes
    event: dict[str, Any]
    attachment_bytes: int


Authenticate = Callable[[], Awaitable[TransportDelivery]]
Authorize = Callable[[asyncpg.Connection, bytes], Awaitable[None]]
Apply = Callable[
    [asyncpg.Connection, KeyScope, dict[str, Any], AsyncIterable[bytes]], Awaitable[None]
]
DrainResult = Literal["absent", "waiting", "drained", "quarantined"]
_INGRESS_SOURCES = frozenset({"telegram", "discord", "web"})


class _Poison(Exception):
    """An invalid stored group whose original ciphertext should be retained."""


class _AlreadySettled(Exception):
    """Undo staged attachments for a duplicate or forgotten event."""


def sealed_destination(config_path: str | None) -> Apply:
    """Persist a normalized delivery using the drain's fenced transaction."""
    from .keyed_work import TransactionPool, scoped_config

    async def persist(conn, scope, event, stream) -> None:
        source = event.get("source")
        source_id = event.get("source_id")
        kind = event.get("kind", "message")
        if (
            not isinstance(source, str)
            or source not in _INGRESS_SOURCES
            or not all(isinstance(value, str) and value for value in (source_id, kind))
        ):
            raise _Poison()
        sender_data = event.get("sender")
        sender = None
        if sender_data is not None:
            if not isinstance(sender_data, dict) or not all(
                isinstance(sender_data.get(key), str) and sender_data[key]
                for key in ("platform", "handle")
            ):
                raise _Poison()
            provider_id = sender_data.get("provider_id")
            if provider_id is not None and (
                not isinstance(provider_id, str)
                or not provider_id.isascii()
                or not provider_id.isdigit()
                or provider_id.startswith("0")
            ):
                raise _Poison()
            aliases = () if provider_id is None else (f"id:{provider_id}",)
            sender = Sender(sender_data["platform"], sender_data["handle"], aliases)
        try:
            occurred = event.get("occurred_at")
            when = datetime.fromisoformat(occurred) if occurred is not None else None
        except (TypeError, ValueError):
            raise _Poison() from None
        if when is not None and when.utcoffset() is None:
            raise _Poison()
        attachments = event.get("attachments", [])
        if not isinstance(attachments, list) or any(
            not isinstance(item, dict)
            or type(item.get("length")) is not int
            or item["length"] <= 0
            or not isinstance(item.get("filename", ""), str)
            for item in attachments
        ):
            raise _Poison()

        versions = dict(scope.refs)
        pool = TransactionPool(conn)
        for category in ("operations", "memory", *(("media",) if attachments else ())):
            if category not in versions:
                raise VaultProtocolError("bad_scope")
        config = await scoped_config(pool, scope, versions["operations"], config_path)
        audit = AuditLog.scoped(pool, scope, ver=versions["operations"])
        events = EventStore.scoped(pool, scope, audit, config.contacts, ver=versions["memory"])
        payload = {key: value for key, value in event.items() if key != "attachments"}
        if not is_allowlisted(config.contacts, source, sender):
            async for _ in stream:
                pass
            await events.ingest(source=source, kind=kind, payload={}, sender=sender)
            return

        body = aiter(stream)
        media_ids = []
        pending = memoryview(b"")
        try:
            async with conn.transaction():
                for item in attachments:
                    data = bytearray()
                    while len(data) < item["length"]:
                        if not pending:
                            pending = memoryview(await anext(body, b""))
                            if not pending:
                                raise _Poison()
                        length = min(item["length"] - len(data), len(pending))
                        data.extend(pending[:length])
                        pending = pending[length:]
                    media = MediaStore.scoped(pool, scope, config.media, ver=versions["media"])
                    ref = await media.put(
                        bytes(data), filename=item.get("filename", ""), origin=source, evict=False
                    )
                    media_ids.append(ref.media_id)
                if pending or await anext(body, None) is not None:
                    raise _Poison()
                if media_ids:
                    payload["media_id"] = media_ids[0]
                    payload["media_ids"] = media_ids
                result = await events.ingest(
                    source=source,
                    kind=kind,
                    payload=payload,
                    sender=sender,
                    occurred_at=when,
                    dedup_key=source_id,
                )
                if not result.stored:
                    raise _AlreadySettled()
                if kind == "message":
                    chat_ref = event.get("chat_ref")
                    message_id = event.get("message_id")
                    if isinstance(chat_ref, str) and isinstance(message_id, str):
                        inbound = DbInboundStates.scoped(pool, scope, ver=versions["operations"])
                        await inbound.record(source, chat_ref, message_id, key=source_id)
        except _AlreadySettled:
            return

    return persist


def _header(vault: bytes, queue: bytes, ver: int, part: int, kind: str) -> bytes:
    return canonical_bytes(
        {
            "v": PROTOCOL_VERSION,
            "vault": b64u_encode(vault),
            "queue_id": b64u_encode(queue),
            "ingress_ver": ver,
            "part": part,
            "kind": kind,
        }
    )


async def _chunks(stream: AsyncIterable[bytes]) -> AsyncIterator[bytes]:
    pending = bytearray()
    async for data in stream:
        remaining = memoryview(data)
        while remaining:
            length = min(_CHUNK_BYTES - len(pending), len(remaining))
            pending.extend(remaining[:length])
            remaining = remaining[length:]
            if len(pending) == _CHUNK_BYTES:
                yield bytes(pending)
                pending.clear()
    if pending:
        yield bytes(pending)


class Ingress:
    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        quota_bytes: int,
        accept_seconds: float = 60,
        apply: Apply | None = None,
    ) -> None:
        if quota_bytes <= 0 or accept_seconds <= 0:
            raise ValueError("ingress quota and timeout must be positive")
        self._store = VaultStore(pool)
        self._quota_bytes = quota_bytes
        self._accept_seconds = accept_seconds
        self._apply = apply

    async def accept(
        self,
        stream: AsyncIterable[bytes],
        authenticate: Authenticate,
        *,
        authorize: Authorize | None = None,
    ) -> bytes:
        delivery = await authenticate()
        if (
            not isinstance(delivery.receipt_token, bytes)
            or len(delivery.receipt_token) != DIGEST_BYTES
            or not isinstance(delivery.event, dict)
            or type(delivery.attachment_bytes) is not int
            or delivery.attachment_bytes < 0
        ):
            raise VaultProtocolError("bad_field")
        try:
            async with asyncio.timeout(self._accept_seconds), self._store.accepting() as conn:
                return await self._accept(conn, stream, delivery, authorize)
        except (asyncpg.PostgresError, asyncpg.InterfaceError, DatabaseUnavailable, TimeoutError):
            raise VaultProtocolError("transient") from None

    async def notice(
        self, receipt: bytes, conversation: bytes, send: Callable[[], Awaitable[None]]
    ) -> None:
        claimed = await self._store.claim_ingress_notice(receipt, conversation)
        if claimed is None:
            return
        async with (
            asyncio.timeout(5),
            self._store.sending_ingress_notice(
                claimed["vault_id"], claimed["epoch"], conversation
            ) as admitted,
        ):
            if admitted:
                await send()

    async def received(self, token: bytes) -> bool:
        return await self._store.has_ingress_receipt(token)

    async def _accept(
        self,
        conn: asyncpg.Connection,
        stream: AsyncIterable[bytes],
        delivery: TransportDelivery,
        authorize: Authorize | None,
    ) -> bytes:
        row = await self._store.ingress_installation(conn)
        if row is None:
            raise VaultProtocolError("unpaired")
        vault = row["vault_id"]
        if authorize is not None:
            await authorize(conn, vault)
        token = delivery.receipt_token
        existing = await self._store.ingress_receipt(conn, vault, token)
        if existing is not None:
            return existing

        manifest = verified_manifest(row)
        ver = next(
            entry["ver"]
            for entry in manifest["entries"]
            if entry["cat"] == "ingress" and entry["status"] == "active"
        )
        public = next(item["public"] for item in manifest["ingress_public"] if item["ver"] == ver)
        raw_key = b64u_decode(public, field="ingress public key", length=X25519_BYTES)
        recipient = KEMKey.from_pyca_cryptography_key(X25519PublicKey.from_public_bytes(raw_key))
        queue = token_bytes(ID_BYTES)
        available = self._quota_bytes - await self._store.ingress_bytes(conn, vault)
        if available <= delivery.attachment_bytes:
            raise VaultProtocolError("transient")
        await self._store.reserve_ingress(conn, vault, queue, ver)
        stored = 0

        async def write(part: int, kind: str, clear: bytes) -> None:
            nonlocal stored
            enc, context = _SUITE.create_sender_context(recipient, _INFO)
            ct = context.seal(clear, _header(vault, queue, ver, part, kind))
            box = canonical_bytes({"enc": b64u_encode(enc), "ct": b64u_encode(ct)})
            stored += len(box)
            if stored > available:
                raise VaultProtocolError("transient")
            await self._store.write_ingress_part(conn, queue, part, kind, box)

        hashes = []
        total = 0
        async for chunk in _chunks(stream):
            total += len(chunk)
            if total > delivery.attachment_bytes:
                raise VaultProtocolError("malformed")
            await write(len(hashes), "chunk", chunk)
            hashes.append(b64u_encode(sha256(chunk).digest()))
        if total != delivery.attachment_bytes:
            raise VaultProtocolError("transient")
        end = canonical_bytes({"hashes": hashes, "length": total, "event": delivery.event})
        await write(len(hashes), "end", end)
        if not await self._store.ingress_manifest_is_current(conn, vault, row["digest"]):
            raise VaultProtocolError("transient")
        await self._store.complete_ingress(conn, vault, token, queue, stored)
        return queue

    async def drain(
        self, task: LeaseWork, queue_id: bytes, apply: Apply | None = None
    ) -> DrainResult:
        scope = task.scope
        destination = apply or self._apply
        if scope.operation != "drain" or destination is None:
            raise VaultProtocolError("bad_scope")
        async with task.transaction() as conn:
            row = await self._store.next_ingress(conn, scope.vault_id)
            if row is None:
                return "absent"
            if row["queue_id"] != queue_id:
                return (
                    "waiting"
                    if await self._store.ingress_pending(conn, scope.vault_id, queue_id)
                    else "absent"
                )
            ver = row["ingress_ver"]

            async def open_part(part: int, kind: str) -> bytes:
                record = await self._store.ingress_part(conn, queue_id, part)
                if record is None or record["kind"] != kind:
                    raise _Poison()
                try:
                    box = loads_strict(record["ciphertext_enc"])
                    if not isinstance(box, dict) or set(box) != {"enc", "ct"}:
                        raise _Poison()
                    enc = b64u_decode(box["enc"], field="enc", length=X25519_BYTES)
                    ct = b64u_decode(box["ct"], field="ct")
                    return scope.open_ingress(
                        ver, enc, ct, _header(scope.vault_id, queue_id, ver, part, kind)
                    )
                except VaultProtocolError:
                    raise _Poison() from None

            try:
                if not row["complete"]:
                    raise _Poison()
                count = await self._store.ingress_part_count(conn, queue_id)
                if not count:
                    raise _Poison()
                try:
                    end = loads_strict(await open_part(count - 1, "end"))
                except VaultProtocolError:
                    raise _Poison() from None
                if (
                    not isinstance(end, dict)
                    or set(end) != {"hashes", "length", "event"}
                    or not isinstance(end["hashes"], list)
                    or len(end["hashes"]) != count - 1
                    or type(end["length"]) is not int
                    or not isinstance(end["event"], dict)
                ):
                    raise _Poison()
                total = 0
                for part, digest in enumerate(end["hashes"]):
                    chunk = await open_part(part, "chunk")
                    total += len(chunk)
                    if b64u_encode(sha256(chunk).digest()) != digest:
                        raise _Poison()
                if total != end["length"]:
                    raise _Poison()
            except _Poison:
                await self._store.settle_ingress(conn, queue_id, poison=True)
                return "quarantined"

            async def stream() -> AsyncIterator[bytes]:
                for part in range(count - 1):
                    yield await open_part(part, "chunk")

            try:
                async with conn.transaction(), aclosing(stream()) as chunks:
                    await destination(conn, scope, end["event"], chunks)
                    async for _ in chunks:
                        raise VaultProtocolError("bad_scope")
            except _Poison:
                await self._store.settle_ingress(conn, queue_id, poison=True)
                return "quarantined"
            await self._store.settle_ingress(conn, queue_id, poison=False)
            return "drained"

    async def drain_pending(self, service: LeaseService) -> DrainResult:
        from .keyed_work import active_refs

        row = await self._store.first_ingress(service.installation.vault_id)
        if row is None:
            return "absent"
        categories = (
            ("memory", "operations", "media") if row["has_media"] else ("memory", "operations")
        )
        versions = active_refs(service, categories)
        ingress_version = row["ingress_ver"]
        if ingress_version is None:
            ingress_version = active_refs(service, ("ingress",))["ingress"]
        async with service.work(
            operation="drain",
            use_case="ingress.drain",
            refs=[("ingress", ingress_version), *versions.items()],
            writes=dict.fromkeys(versions.items(), 32),
            ttl=30,
        ) as task:
            return await self.drain(task, row["queue_id"])


async def watch_ingress(runtime: VaultRuntime, ingress: Ingress) -> None:
    while True:
        await asyncio.sleep(1)
        try:
            service = runtime.serving()
            await asyncio.create_task(ingress.drain_pending(service))
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
        except VaultProtocolError:
            continue
        except Exception:
            logging.getLogger(__name__).warning("An encrypted delivery remains pending.")
