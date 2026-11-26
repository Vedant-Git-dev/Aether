"""Application ownership of the serving session and its current lease service."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from hashlib import sha256

import asyncpg

from ..db_resilience import DatabaseUnavailable
from .errors import VaultProtocolError
from .schema import (
    DIGEST_BYTES,
    P256_PUBLIC_BYTES,
    b64u_decode,
    b64u_encode,
    canonical_bytes,
    loads_strict,
    normalize_origin,
)
from .scope import VaultLocked
from .service import Installation, LeaseService
from .signing import RequestSigner, verify_es256
from .store import ServingBoot, VaultStore

STORAGE_ERRORS = (
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
    DatabaseUnavailable,
    OSError,
    TimeoutError,
)
_CHECK_INTERVAL = 1.0


def verified_manifest(row: asyncpg.Record) -> dict:
    pins = loads_strict(row["pins"])
    if not isinstance(pins, dict):
        raise VaultProtocolError("stale_manifest")
    phone = b64u_decode(pins.get("phone_pub"), field="phone_pub", length=P256_PUBLIC_BYTES)
    kid = pins.get("server_kid")
    b64u_decode(kid, field="server_kid", length=DIGEST_BYTES)
    manifest = verify_es256(
        loads_strict(row["manifest"]), pinned_public=phone, expected_type="manifest"
    )
    if (
        sha256(canonical_bytes(manifest)).digest() != row["digest"]
        or manifest["vault"] != b64u_encode(row["vault_id"])
        or manifest["device"] != b64u_encode(row["device_id"])
        or manifest["epoch"] > row["epoch"]
        or manifest["revision"] != row["revision"]
    ):
        raise VaultProtocolError("stale_manifest")
    return manifest


def _installation(row: asyncpg.Record) -> Installation:
    manifest = verified_manifest(row)
    pins = loads_strict(row["pins"])
    return Installation(
        row["vault_id"],
        row["device_id"],
        row["generation"],
        row["epoch"],
        row["digest"],
        pins["server_kid"],
        b64u_decode(pins["phone_pub"], field="phone_pub", length=P256_PUBLIC_BYTES),
        {(entry["cat"], entry["ver"]): entry["status"] for entry in manifest["entries"]},
        frozenset(entry["domain"] for entry in manifest.get("historical_lookups", ())),
        normalize_origin(pins.get("origin")),
    )


class VaultRuntime:
    def __init__(
        self, store: VaultStore, signer: RequestSigner | None, *, pending_wait: float = 0
    ) -> None:
        self.store = store
        self.pending_wait = pending_wait
        self.pending_poll = asyncio.Lock()
        self._signer = signer
        self._service: LeaseService | None = None
        self._retiring: asyncio.Task | None = None
        self._worker: asyncio.Task | None = None
        self._leader: ServingBoot | None = None
        self._updates = asyncio.Lock()
        self._transitions: set[asyncio.Task[bool]] = set()
        self._stopping = False
        self._fenced_epoch: tuple[bytes, int] | None = None

    async def __aenter__(self) -> VaultRuntime:
        if self._signer is not None:
            self._worker = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._stopping = True
        await asyncio.gather(*self._transitions, return_exceptions=True)
        if self._worker is not None:
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker

    def serving(self) -> LeaseService:
        if self._stopping or self._service is None or self._service.closed:
            raise VaultProtocolError("unavailable")
        return self._service

    async def device_service(self, token_hash: bytes, origin: str | None) -> LeaseService:
        session = await self.store.device_session(token_hash)
        if session is None:
            raise VaultProtocolError("unauthorized")
        if session["state"] == "locked":
            raise VaultProtocolError("locked")
        service = self.serving()
        item = service.installation
        if origin is not None and origin != item.origin:
            raise VaultProtocolError("unauthorized")
        if (session["vault_id"], session["device_id"], session["epoch"], session["generation"]) != (
            item.vault_id,
            item.device_id,
            item.epoch,
            item.generation,
        ):
            raise VaultProtocolError("stale_generation")
        return service

    async def apply_verified_control(
        self, *, vault_id: bytes, epoch: int, generation: int, state: str
    ) -> bool:
        """Fence local work and join its cleanup before acknowledging a verified control."""
        return await self._transition(
            vault_id,
            epoch,
            generation,
            lambda boot: self.store.set_state(
                vault_id,
                state,
                expected_epoch=epoch,
                expected_generation=generation,
                boot=boot,
            ),
            fence_epoch=state in {"locked", "revoked"},
        )

    async def publish_verified_manifest(
        self,
        *,
        vault_id: bytes,
        expected_epoch: int,
        expected_generation: int,
        expected_revision: int,
        expected_digest: bytes,
        revision: int,
        digest: bytes,
        previous_digest: bytes,
        manifest: bytes,
    ) -> bool:
        return await self._transition(
            vault_id,
            expected_epoch,
            expected_generation,
            lambda boot: self.store.publish_manifest(
                vault_id=vault_id,
                expected_epoch=expected_epoch,
                expected_generation=expected_generation,
                expected_revision=expected_revision,
                expected_digest=expected_digest,
                revision=revision,
                digest=digest,
                previous_digest=previous_digest,
                manifest=manifest,
                boot=boot,
            ),
        )

    async def _transition(
        self,
        vault_id: bytes,
        epoch: int,
        generation: int,
        persist: Callable[[ServingBoot], Awaitable[bool]],
        *,
        fence_epoch: bool = False,
    ) -> bool:
        if self._stopping:
            raise VaultProtocolError("unavailable")
        service = self._service
        if fence_epoch and service is not None:
            item = service.installation
            if (item.vault_id, item.epoch, item.generation) == (vault_id, epoch, generation):
                self._fenced_epoch = (vault_id, epoch)
                self._start_retirement()
        task = asyncio.create_task(
            self._apply_transition(vault_id, epoch, generation, persist, fence_epoch)
        )
        self._transitions.add(task)
        task.add_done_callback(self._transition_done)
        return await asyncio.shield(task)

    def _transition_done(self, task: asyncio.Task[bool]) -> None:
        self._transitions.discard(task)
        if not task.cancelled():
            task.exception()

    async def _apply_transition(
        self,
        vault_id: bytes,
        epoch: int,
        generation: int,
        persist: Callable[[ServingBoot], Awaitable[bool]],
        fence_epoch: bool,
    ) -> bool:
        async with self._updates:
            boot = self._leader
            if boot is None:
                raise VaultProtocolError("unavailable")
            await boot.check()
            row = await self.store.installation()
            if row is None or (row["vault_id"], row["epoch"], row["generation"]) != (
                vault_id,
                epoch,
                generation,
            ):
                return False
            if fence_epoch:
                self._fenced_epoch = (vault_id, epoch)
            self._start_retirement()
            try:
                return await persist(boot)
            finally:
                await self._retire()

    async def _run(self) -> None:
        while True:
            try:
                async with self.store.serving_boot() as boot:
                    self._leader = boot
                    try:
                        while not boot.lost.is_set():
                            async with self._updates:
                                await self._refresh(boot)
                            try:
                                await asyncio.wait_for(boot.lost.wait(), _CHECK_INTERVAL)
                            except TimeoutError:
                                continue
                    finally:
                        self._leader = None
                        await self._retire()
            except (VaultProtocolError, *STORAGE_ERRORS):
                pass
            await asyncio.sleep(_CHECK_INTERVAL)

    async def _refresh(self, boot: ServingBoot) -> None:
        await boot.check()
        row = await self.store.installation()
        service = self._service
        if service is not None and not service.closed:
            with suppress(VaultProtocolError):
                service._require_live()
        if (
            row is None
            or row["state"] != "available"
            or (row["vault_id"], row["epoch"]) == self._fenced_epoch
            or self._stopping
        ):
            await self._retire()
        elif (
            service is None
            or service.closed
            or row["generation"] != service.installation.generation
        ):
            await self._retire()
            # A paired installation with unavailable crypto or invalid pins serves no work.
            signer = self._signer
            assert signer is not None
            with suppress(VaultLocked, ValueError, RecursionError):
                self._service = LeaseService(self.store, boot, _installation(row), signer)

    def _start_retirement(self) -> None:
        service, self._service = self._service, None
        if service is not None:
            service.close()
            self._retiring = asyncio.create_task(service.drain())

    async def _retire(self) -> None:
        self._start_retirement()
        if self._retiring is not None:
            await asyncio.shield(self._retiring)
            self._retiring = None
