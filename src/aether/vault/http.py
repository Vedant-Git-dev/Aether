"""Device-only lease endpoints. Callback ciphertext never leaves these routes."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from hashlib import sha256

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from .errors import VaultProtocolError
from .runtime import STORAGE_ERRORS, VaultRuntime
from .schema import MEDIA_TYPE, NONCE_BYTES, b64u_decode, loads_strict, vault_path
from .scope import VaultError

router = APIRouter()
_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
_MAX_ENVELOPE_BYTES = 128 * 1024


def _error(error: VaultProtocolError) -> Response:
    return JSONResponse(error.payload(), status_code=error.status, headers=_HEADERS)


def bearer_hash(request: Request) -> bytes:
    credentials = request.headers.getlist("authorization")
    if len(credentials) != 1:
        raise VaultProtocolError("unauthorized")
    scheme, _, token = credentials[0].partition(" ")
    if scheme.lower() != "bearer":
        raise VaultProtocolError("unauthorized")
    try:
        return sha256(b64u_decode(token, field="token", length=NONCE_BYTES)).digest()
    except VaultProtocolError:
        raise VaultProtocolError("unauthorized") from None


async def _device(request: Request):
    token_hash = bearer_hash(request)
    origins = request.headers.getlist("origin")
    if len(origins) > 1:
        raise VaultProtocolError("unauthorized")
    runtime: VaultRuntime | None = getattr(request.app.state, "vault_runtime", None)
    if runtime is None:
        raise VaultProtocolError("unavailable")
    return runtime, await runtime.device_service(token_hash, origins[0] if origins else None)


@router.get(vault_path("status"))
async def status(request: Request) -> Response:
    runtime: VaultRuntime = request.app.state.vault_runtime
    try:
        if request.headers.getlist("authorization"):
            token = bearer_hash(request)
            session = await runtime.store.active_session(token, "device")
            if session is None:
                session = await runtime.store.active_session(token, "panel")
            if session is None:
                raise VaultProtocolError("unauthorized")
            body = {"state": session["state"], "epoch": session["epoch"]}
        else:
            body = {"pairing_required": await runtime.store.installation() is None}
        return JSONResponse(body, headers=_HEADERS)
    except VaultProtocolError as exc:
        return _error(exc)
    except STORAGE_ERRORS:
        return _error(VaultProtocolError("unavailable"))


async def _disconnected(request: Request) -> None:
    while (await request.receive())["type"] != "http.disconnect":
        pass


async def _pending_body(request: Request) -> bytes | None:
    runtime, service = await _device(request)
    if not runtime.pending_wait:
        return await service.pending()
    if runtime.pending_poll.locked():
        raise VaultProtocolError("conflict")
    async with runtime.pending_poll:
        clock = asyncio.get_running_loop().time
        deadline = clock() + runtime.pending_wait
        disconnect = asyncio.create_task(_disconnected(request))
        try:
            while True:
                changed = service.pending_changed
                body = await service.pending()
                remaining = deadline - clock()
                if body is not None or remaining <= 0 or disconnect.done():
                    return body if not disconnect.done() else None
                change = asyncio.create_task(changed.wait())
                try:
                    await asyncio.wait(
                        (change, disconnect),
                        timeout=min(remaining, 1),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    change.cancel()
                    with suppress(asyncio.CancelledError):
                        await change
                if disconnect.done():
                    return None
                _, current = await _device(request)
                if current is not service:
                    raise VaultProtocolError("stale_generation")
        finally:
            disconnect.cancel()
            with suppress(asyncio.CancelledError):
                await disconnect


@router.get(vault_path("pending"))
async def pending(request: Request) -> Response:
    try:
        body = await _pending_body(request)
        if body is None:
            return Response(status_code=204, headers=_HEADERS)
        return Response(body, media_type=MEDIA_TYPE, headers=_HEADERS)
    except VaultProtocolError as exc:
        return _error(exc)
    except STORAGE_ERRORS:
        return _error(VaultProtocolError("unavailable"))


async def _callback(request: Request, kind: str) -> Response:
    try:
        await _device(request)
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != MEDIA_TYPE:
            raise VaultProtocolError("malformed")
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > _MAX_ENVELOPE_BYTES:
                raise VaultProtocolError("malformed")
            raw.extend(chunk)
        try:
            envelope = loads_strict(raw.decode("utf-8"))
        except VaultProtocolError:
            raise
        except (ValueError, RecursionError):
            raise VaultProtocolError("malformed") from None
        _, service = await _device(request)
        await service.callback(envelope, kind=kind)
        return Response(status_code=204, headers=_HEADERS)
    except VaultProtocolError as exc:
        return _error(exc)
    except RecursionError:
        return _error(VaultProtocolError("malformed"))
    except STORAGE_ERRORS:
        return _error(VaultProtocolError("unavailable"))
    except VaultError:
        return _error(VaultProtocolError("category_unavailable"))


@router.post(vault_path("grant"))
async def grant(request: Request) -> Response:
    return await _callback(request, "lease_grant")


@router.post(vault_path("deny"))
async def deny(request: Request) -> Response:
    return await _callback(request, "lease_denied")
