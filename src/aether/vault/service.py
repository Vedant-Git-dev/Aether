"""Boot-local lease requests and atomic grant installation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from hashlib import sha256
from secrets import token_bytes
from typing import Any

from .errors import VaultProtocolError
from .lease_work import LeaseWork
from .native import NativeKeyHandle
from .native_grant import NativeBootKey, NativeIngressKey
from .policy import policy
from .schema import (
    HISTORICAL_LOOKUP_CATEGORIES,
    ID_BYTES,
    NONCE_BYTES,
    b64u_decode,
    b64u_encode,
    canonical_bytes,
    validate_use_case,
)
from .scope import KeyScope, VaultClosed, normalize_refs
from .signing import RequestSigner, verify_es256
from .store import ServingBoot, VaultStore

Ref = tuple[str, int]
_BOUND_FIELDS = ("vault", "device", "epoch", "boot", "task", "request", "nonce")
_READ_OLD = frozenset({"drain", "panel_read", "maintenance"})


@dataclass(frozen=True)
class Installation:
    """Verified pairing and manifest snapshot for the serving generation."""

    vault_id: bytes
    device_id: bytes
    generation: int
    epoch: int
    digest: bytes
    server_kid: str
    phone_public: bytes
    entries: Mapping[Ref, str]
    historical_domains: frozenset[str] = frozenset()
    origin: str | None = None


@dataclass(eq=False)
class LeaseTicket:
    """A single admitted task's request and eventual scoped grant."""

    body: dict[str, Any]
    wire: bytes
    digest: str
    sent: float
    accept_deadline: float
    writes: dict[Ref, int]
    ready: asyncio.Event
    timer: asyncio.TimerHandle | None = None
    scope: KeyScope | None = None
    error: str = "expired_request"
    previous: LeaseTicket | None = None
    renewal_at: float = 0
    expires: float = 0

    async def wait(self) -> KeyScope:
        await self.ready.wait()
        if self.scope is None or self.scope.closed:
            raise VaultProtocolError(self.error)
        return self.scope


class LeaseService:
    """Serve only requests created by this boot for admitted work."""

    def __init__(
        self,
        store: VaultStore,
        boot: ServingBoot,
        installation: Installation,
        signer: RequestSigner,
        *,
        wall: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if signer.kid != installation.server_kid:
            raise VaultProtocolError("bad_signature")
        if boot.lost.is_set():
            raise VaultProtocolError("stale_generation")
        self._store = store
        self._boot = boot
        self._authorization_revision = boot.authorization_revision
        self._installation = installation
        self._signer = signer
        self._wall = wall
        self._monotonic = monotonic
        self._boottime = (
            (lambda: time.clock_gettime(time.CLOCK_BOOTTIME))
            if monotonic is time.monotonic and hasattr(time, "CLOCK_BOOTTIME")
            else monotonic
        )
        self._clock_offset = (wall() - monotonic(), self._boottime() - monotonic())
        self._boot_id = b64u_encode(token_bytes(ID_BYTES))
        self._recipient = NativeBootKey()
        self._tickets: dict[bytes, LeaseTicket] = {}
        self._renewals: dict[bytes, LeaseTicket] = {}
        self._owners: dict[bytes, tuple[asyncio.Task, asyncio.Event]] = {}
        self._pending_changed = asyncio.Event()
        self._receipts: dict[str, tuple[bytes, int, float]] = {}
        self._callbacks = asyncio.Lock()
        self._closed = False
        self._watch = asyncio.create_task(self._watch_leadership())

    @property
    def installation(self) -> Installation:
        return self._installation

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def pending_changed(self) -> asyncio.Event:
        return self._pending_changed

    def _notify_pending(self) -> None:
        self._pending_changed.set()
        self._pending_changed = asyncio.Event()

    async def _watch_leadership(self) -> None:
        await self._boot.lost.wait()
        self.close()

    def _require_live(self) -> None:
        offsets = (self._wall() - self._monotonic(), self._boottime() - self._monotonic())
        if (
            self._closed
            or self._boot.lost.is_set()
            or self._authorization_revision != self._boot.authorization_revision
            or any(
                abs(current - previous) >= 1
                for current, previous in zip(offsets, self._clock_offset, strict=True)
            )
        ):
            self.close()
            raise VaultProtocolError("stale_generation")
        self._clock_offset = offsets

    def _fence(self, *, check: Callable[[], None] | None = None):
        self._require_live()
        item = self._installation
        return self._boot.transaction(
            vault_id=item.vault_id,
            generation=item.generation,
            epoch=item.epoch,
            manifest_digest=item.digest,
            check=check,
        )

    async def begin(
        self,
        *,
        task: bytes,
        operation: str,
        use_case: str,
        refs: Iterable[Ref],
        ttl: int,
        writes: Mapping[Ref, int] | None = None,
    ) -> LeaseTicket:
        refs = normalize_refs(refs)
        allowed = validate_use_case(use_case, operation)
        if not refs or any(cat not in allowed for cat, _ in refs):
            raise VaultProtocolError("bad_scope")
        item = self._installation
        categories = {cat for cat, _ in refs}
        if any(
            HISTORICAL_LOOKUP_CATEGORIES[domain] in categories for domain in item.historical_domains
        ):
            raise VaultProtocolError("category_unavailable")
        writes = dict(writes or {})
        for ref in refs:
            status = item.entries.get(ref)
            if status != "active" and (
                status != "read_only" or operation not in _READ_OLD or ref in writes
            ):
                raise VaultProtocolError("bad_scope")
        if set(writes) - set(refs) or any(
            cat == "ingress" or type(count) is not int or count <= 0
            for (cat, _), count in writes.items()
        ):
            raise VaultProtocolError("bad_scope")
        async with self._fence():
            pass
        self._require_live()
        current = self._tickets.get(task)
        wire_refs = [{"cat": cat, "ver": ver} for cat, ver in refs]
        if current is not None:
            body = current.body
            if (body["operation"], body["use_case"], body["refs"], body["ttl"], current.writes) != (
                operation,
                use_case,
                wire_refs,
                ttl,
                writes,
            ):
                raise VaultProtocolError("conflict")
            return current
        return self._new_ticket(task, operation, use_case, wire_refs, ttl, writes)

    def _new_ticket(self, task, operation, use_case, wire_refs, ttl, writes, *, previous=None):
        item = self._installation
        sent, iat = self._monotonic(), int(self._wall())
        window = policy().accept_window_seconds
        body = {
            "v": 1,
            "type": "lease_request",
            "vault": b64u_encode(item.vault_id),
            "device": b64u_encode(item.device_id),
            "epoch": item.epoch,
            "boot": self._boot_id,
            "task": b64u_encode(task),
            "operation": operation,
            "use_case": use_case,
            "request": b64u_encode(token_bytes(ID_BYTES)),
            "nonce": b64u_encode(token_bytes(NONCE_BYTES)),
            "iat": iat,
            "accept_until": iat + window,
            "refs": wire_refs,
            "ttl": ttl,
            "epk": b64u_encode(self._recipient.public),
            "manifest_digest": b64u_encode(item.digest),
        }
        wire = canonical_bytes(self._signer.sign_request(body))
        deadline = sent + min(window, ttl)
        ticket = LeaseTicket(
            body,
            wire,
            b64u_encode(sha256(canonical_bytes(body)).digest()),
            sent,
            deadline,
            writes,
            asyncio.Event(),
            previous=previous,
        )
        ticket.timer = asyncio.get_running_loop().call_later(
            deadline - sent, self._expire, task, ticket
        )
        (self._tickets if previous is None else self._renewals)[task] = ticket
        self._notify_pending()
        return ticket

    @asynccontextmanager
    async def work(
        self,
        *,
        operation: str,
        use_case: str,
        refs: Iterable[Ref],
        ttl: int,
        writes: Mapping[Ref, int] | None = None,
    ) -> AsyncIterator[LeaseWork]:
        """Own all work and renewals under one task and one stable key scope."""
        owner = asyncio.current_task()
        assert owner is not None
        task = token_bytes(ID_BYTES)
        done = asyncio.Event()
        self._owners[task] = (owner, done)
        try:
            requested = tuple(refs)
            while True:
                ticket = await self.begin(
                    task=task,
                    operation=operation,
                    use_case=use_case,
                    refs=requested,
                    ttl=ttl,
                    writes=writes,
                )
                try:
                    scope = await ticket.wait()
                    break
                except VaultProtocolError as exc:
                    if exc.code != "expired_request":
                        raise
            async with LeaseWork(self, scope, ticket).running() as work:
                yield work
        finally:
            self.end(task)
            self._owners.pop(task, None)
            done.set()

    async def _renew(self, task: bytes, previous: LeaseTicket) -> LeaseTicket:
        async with self._fence():
            pass
        self._require_live()
        if (
            self._tickets.get(task) is not previous
            or previous.scope is None
            or previous.scope.closed
        ):
            raise VaultProtocolError("conflict")
        self._check_scope(task, previous, previous.expires)
        body = previous.body
        return self._new_ticket(
            task,
            body["operation"],
            body["use_case"],
            body["refs"],
            body["ttl"],
            previous.writes,
            previous=previous,
        )

    async def pending(self) -> bytes | None:
        async with self._fence():
            pass
        self._require_live()
        for task, ticket in [*self._tickets.items(), *self._renewals.items()]:
            if ticket.scope is None:
                if self._active(ticket):
                    return ticket.wire
                self._expire(task, ticket)
        return None

    def _active(self, ticket: LeaseTicket) -> bool:
        return (
            self._monotonic() < ticket.accept_deadline
            and self._wall() < ticket.body["accept_until"]
        )

    async def callback(self, envelope: Any, *, kind: str) -> None:
        if kind not in ("lease_grant", "lease_denied"):
            raise ValueError("unexpected lease callback type")
        self._require_live()
        body = verify_es256(
            envelope, pinned_public=self._installation.phone_public, expected_type=kind
        )
        task = b64u_decode(body["task"], field="task", length=ID_BYTES)
        async with self._callbacks:
            async with self._fence():
                pass
            self._require_live()
            if body["boot"] != self._boot_id:
                raise VaultProtocolError("stale_generation")
            digest = sha256(canonical_bytes(body)).digest()
            now, monotonic = self._wall(), self._monotonic()
            self._receipts = {
                request: receipt
                for request, receipt in self._receipts.items()
                if now < receipt[1] and monotonic < receipt[2]
            }
            if receipt := self._receipts.get(body["request"]):
                if digest != receipt[0]:
                    raise VaultProtocolError("replay")
                return
            ticket = self._renewals.get(task) or self._tickets.get(task)
            if ticket is None or ticket.scope is not None:
                raise VaultProtocolError("conflict")
            if (
                any(body[field] != ticket.body[field] for field in _BOUND_FIELDS)
                or body["request_digest"] != ticket.digest
            ):
                raise VaultProtocolError("conflict")
            if not self._active(ticket):
                self._expire(task, ticket)
                raise VaultProtocolError("expired_request")
            skew = policy().clock_skew_seconds
            if not ticket.body["iat"] - skew <= body["iat"] <= now + skew:
                raise VaultProtocolError("expired_request")
            if kind == "lease_denied":
                self._reject(task, ticket, "denied")
                return
            if (
                body["refs"] != ticket.body["refs"]
                or body["manifest_digest"] != ticket.body["manifest_digest"]
            ):
                raise VaultProtocolError("bad_scope")
            ttl = ticket.body["ttl"]
            if body["exp"] != min(body["iat"], ticket.body["iat"]) + ttl or now >= body["exp"]:
                raise VaultProtocolError("expired_request")
            expires = min(monotonic + body["exp"] - now, ticket.sent + ttl)
            await self._install(task, ticket, body, expires)
            self._receipts[body["request"]] = (
                digest,
                ticket.body["accept_until"],
                ticket.sent + policy().accept_window_seconds,
            )

    async def _install(
        self, task: bytes, ticket: LeaseTicket, body: dict[str, Any], expires: float
    ) -> None:
        vault_id = self._installation.vault_id
        async with self._fence() as connection:
            writes = {
                (cat, ver): await self._store.reserve_aead(
                    vault_id=vault_id,
                    cat=cat,
                    ver=ver,
                    count=count,
                    connection=connection,
                )
                for (cat, ver), count in ticket.writes.items()
            }
            self._check_install(task, ticket, expires)
        async with self._boot.publication:
            async with self._fence():
                pass
            self._check_install(task, ticket, expires)
            # Revocation can see every opened slot before this coroutine yields.
            slots = self._recipient.open_grant_slots(body)
            try:
                self._check_install(task, ticket, expires)
                self._publish(task, ticket, slots, writes, expires)
            except BaseException:
                for slot in slots.values():
                    slot.close()
                raise

    def _publish(self, task, ticket, slots, writes, expires) -> None:
        vault_id = self._installation.vault_id
        handles = {
            ref: NativeIngressKey(ref[1], slot)
            if ref[0] == "ingress"
            else NativeKeyHandle(
                vault_id=vault_id, cat=ref[0], ver=ref[1], slot=slot, writes=writes.get(ref)
            )
            for ref, slot in slots.items()
        }

        def guard():
            self._check_scope(task, ticket, expires)

        if ticket.previous is None:
            ticket.scope = KeyScope(
                vault_id=vault_id,
                task=task,
                operation=ticket.body["operation"],
                handles=handles,
                guard=guard,
            )
        else:
            previous = ticket.previous
            assert previous.scope is not None
            previous.scope._renew(handles, guard)
            ticket.scope = previous.scope
            previous.scope = None
            if previous.timer is not None:
                previous.timer.cancel()
            self._renewals.pop(task)
            self._tickets[task] = ticket
            ticket.previous = None
        if ticket.timer is not None:
            ticket.timer.cancel()
        ticket.timer = asyncio.get_running_loop().call_later(
            expires - self._monotonic(),
            self._expire,
            task,
            ticket,
        )
        now = self._monotonic()
        rules = policy()
        ticket.expires = expires
        ticket.renewal_at = (
            now + (expires - now) * rules.renewal_numerator / rules.renewal_denominator
        )
        ticket.ready.set()

    def _check_install(self, task: bytes, ticket: LeaseTicket, expires: float) -> None:
        self._require_live()
        outstanding = self._renewals if ticket.previous is not None else self._tickets
        if outstanding.get(task) is not ticket:
            raise VaultProtocolError("conflict")
        if ticket.previous is not None and self._monotonic() >= ticket.previous.expires:
            self._expire(task, ticket.previous)
            raise VaultProtocolError("expired_request")
        if not self._active(ticket) or self._monotonic() >= expires:
            raise VaultProtocolError("expired_request")

    def _expire(self, task: bytes, ticket: LeaseTicket) -> None:
        self._reject(task, ticket, "expired_request")

    def _reject(self, task: bytes, ticket: LeaseTicket, code: str) -> None:
        if self._tickets.get(task) is ticket:
            self._finish(task, code)
            if ticket.scope is not None:
                self._cancel_owner(task)
        elif self._renewals.get(task) is ticket:
            self._close_ticket(self._renewals.pop(task), code)

    def _check_scope(self, task: bytes, ticket: LeaseTicket, expires: float) -> None:
        try:
            self._require_live()
        except VaultProtocolError as exc:
            raise VaultClosed("the serving boot lost leadership or clock continuity") from exc
        if self._monotonic() >= expires:
            self._expire(task, ticket)
            raise VaultClosed("the task grant expired")

    def end(self, task: bytes) -> None:
        """Fence a completing or cancelled task before it can accept another grant."""
        self._finish(task, "conflict")

    def _finish(self, task: bytes, code: str) -> None:
        for tickets in (self._tickets, self._renewals):
            if ticket := tickets.pop(task, None):
                self._close_ticket(ticket, code)

    @staticmethod
    def _close_ticket(ticket: LeaseTicket, code: str) -> None:
        if ticket.timer is not None:
            ticket.timer.cancel()
        if ticket.scope is not None:
            ticket.scope.close()
        ticket.error = code
        ticket.ready.set()

    def _cancel_owner(self, task: bytes) -> None:
        pair = self._owners.get(task)
        if pair is not None and pair[0] is not asyncio.current_task():
            pair[0].cancel()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._notify_pending()
        if asyncio.current_task() is not self._watch:
            self._watch.cancel()
        for task in list(self._tickets):
            self._finish(task, "stale_generation")
        for task in self._owners:
            self._cancel_owner(task)
        self._receipts.clear()
        self._recipient.close()

    async def drain(self) -> None:
        """Fence immediately, then wait for owned tasks and their resources to settle."""
        self.close()
        await asyncio.gather(*(done.wait() for _, done in self._owners.values()))
