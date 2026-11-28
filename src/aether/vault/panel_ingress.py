"""Authenticated panel WebSocket capture with durable retry receipts."""

from __future__ import annotations

import asyncio
from hashlib import sha256
from uuid import UUID

from starlette.types import Receive, Scope, Send
from starlette.websockets import WebSocket, WebSocketDisconnect

from .errors import VaultProtocolError
from .ingress import TransportDelivery
from .keyed_work import TransactionPool
from .runtime import STORAGE_ERRORS
from .schema import NONCE_BYTES, b64u_decode, loads_strict
from .store import VaultStore


async def _object(socket: WebSocket, fields: set[str]) -> dict:
    frame = await socket.receive()
    if frame["type"] == "websocket.disconnect":
        raise WebSocketDisconnect()
    text = frame.get("text")
    if not isinstance(text, str):
        raise VaultProtocolError("malformed")
    obj = loads_strict(text)
    if not isinstance(obj, dict) or obj.keys() != fields:
        raise VaultProtocolError("bad_field")
    return obj


async def panel_socket(scope: Scope, receive: Receive, send: Send) -> None:
    socket = WebSocket(scope, receive=receive, send=send)
    await socket.receive()
    origins = socket.headers.getlist("origin")
    runtime = getattr(socket.app.state, "vault_runtime", None)
    try:
        installation = await runtime.store.installation() if runtime and len(origins) == 1 else None
        if (
            installation is None
            or scope.get("query_string")
            or origins != [loads_strict(installation["pins"])["origin"]]
        ):
            await send({"type": "websocket.close", "code": 1008})
            return

        await socket.accept()
        async with asyncio.timeout(5):
            auth = await _object(socket, {"token"})
            token = sha256(b64u_decode(auth["token"], field="token", length=NONCE_BYTES)).digest()
            if await runtime.store.active_session(token, "panel") is None:
                raise VaultProtocolError("unauthorized")

        ingress = socket.app.state.ingress

        async def authorize(conn, vault):
            # Lock the installation before the session to serialize with the
            # control transaction. Both remain locked through receipt commit.
            current = await VaultStore.lock_ingress_installation(conn, vault)
            session = await VaultStore(TransactionPool(conn)).active_session(
                token, "panel", for_delivery=True
            )
            if (
                not current
                or session is None
                or session["vault_id"] != vault
                or vault != installation["vault_id"]
            ):
                raise VaultProtocolError("unauthorized")

        while True:
            message = await _object(socket, {"id", "text"})
            event_id = message["id"]
            text = message["text"]
            if not isinstance(event_id, str) or not isinstance(text, str) or not text.strip():
                raise VaultProtocolError("bad_field")
            try:
                stable_id = UUID(event_id)
            except ValueError:
                raise VaultProtocolError("bad_field") from None
            if stable_id.version != 4 or str(stable_id) != event_id:
                raise VaultProtocolError("bad_field")

            delivery = TransportDelivery(
                sha256(b"web:" + stable_id.bytes).digest(),
                {
                    "source": "web",
                    "source_id": f"web:{event_id}",
                    "chat_ref": "web",
                    "message_id": event_id,
                    "text": text,
                },
                0,
            )

            async def authenticate(delivery=delivery):
                return delivery

            await ingress.accept(_empty(), authenticate, authorize=authorize)
            await socket.send_json({"type": "receipt", "id": event_id})
    except WebSocketDisconnect:
        return
    except TimeoutError:
        await socket.close(code=1008)
    except VaultProtocolError as exc:
        await socket.close(code=1011 if exc.code == "transient" else 1008)
    except STORAGE_ERRORS:
        await socket.close(code=1011)


async def _empty():
    for part in ():
        yield part
