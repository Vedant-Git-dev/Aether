"""The web chat surface: one WebSocket endpoint, token-guarded, with
server-side history persisted encrypted. Reconnection (exponential backoff
with jitter) lives in the client, web/index.html.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

import asyncpg
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..connectors.base import InboundMessage
from ..memory.crypto import Cipher
from ..memory.rows import CipherRows, Rows, ScopedRows
from ..vault.scope import KeyScope

log = logging.getLogger("aether.chat")

router = APIRouter()


class ChatHistory:
    """The chat transcript, encrypted at rest like everything personal."""

    def __init__(
        self, pool: asyncpg.Pool, cipher: Cipher | None, *, rows: Rows | None = None
    ) -> None:
        if (cipher is None) == (rows is None):
            raise ValueError("ChatHistory needs exactly one of cipher or rows")
        self._rows: Rows = rows if rows is not None else CipherRows(cipher)
        self._scoped = self._rows.scoped
        self._pool = pool

    @classmethod
    def scoped(cls, pool: asyncpg.Pool, scope: KeyScope, *, ver: int = 1) -> ChatHistory:
        """Explicit scoped construction. The caller owns the task and scope."""
        return cls(pool, None, rows=ScopedRows(scope, cat="memory", ver=ver))

    async def append(self, surface: str, direction: str, text: str) -> None:
        self._rows.preflight()
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO chat_messages (direction, payload_enc,"
                " surface_token, key_version)"
                " VALUES ($1, $2, $3, $4) RETURNING id",
                direction,
                b"",  # placeholder until the id exists; replaced below, same transaction
                self._rows.token("chat.surface", "source", surface),
                self._rows.key_version,
            )
            blob = self._rows.seal(
                {"text": text, "surface": surface},
                table="chat_messages",
                column="payload_enc",
                row_id=row["id"],
                aad=f"chat_messages:payload_enc:{row['id']}",
            )
            await conn.execute(
                "UPDATE chat_messages SET payload_enc = $1 WHERE id = $2",
                blob,
                row["id"],
            )

    async def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        self._rows.preflight()
        rows = await self._pool.fetch(
            "SELECT id, direction, created_at, payload_enc"
            " FROM chat_messages ORDER BY id DESC LIMIT $1",
            limit,
        )
        out: list[dict[str, Any]] = []
        for row in reversed(rows):  # oldest first for display
            try:
                payload = self._rows.open(
                    row["payload_enc"],
                    table="chat_messages",
                    column="payload_enc",
                    row_id=row["id"],
                    aad=f"chat_messages:payload_enc:{row['id']}",
                )
            except Exception:
                if self._scoped:
                    raise
                continue
            out.append(
                {
                    "surface": str(payload.get("surface", "")),
                    "direction": row["direction"],
                    "text": payload.get("text", ""),
                    "at": row["created_at"].isoformat(),
                }
            )
        return out


class ChatHub:
    """Every connected web client. broadcast() fans out to all of them and
    persists the outgoing line; a dead socket drops out quietly."""

    def __init__(self, history: ChatHistory) -> None:
        self._history = history
        self._clients: set[WebSocket] = set()

    @property
    def connected(self) -> int:
        return len(self._clients)

    def connect(self, websocket: WebSocket) -> None:
        self._clients.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)

    async def broadcast(self, text: str, *, persist: bool = True) -> None:
        if persist:
            await self._history.append("web", "out", text)
        dead: list[WebSocket] = []
        for websocket in list(self._clients):
            try:
                await websocket.send_json({"type": "chat", "text": text})
            except Exception:
                dead.append(websocket)
        for websocket in dead:
            self.disconnect(websocket)


def _authorized(websocket: WebSocket, token: str | None, expected: str) -> bool:
    candidate = token or websocket.headers.get("x-aether-token", "")
    return bool(candidate) and hmac.compare_digest(candidate, expected)


@router.websocket("/ws")
async def chat_ws(websocket: WebSocket) -> None:
    """Chat with the agent from the browser. History replays on connect;
    everything the user types goes to the agent loop like any other surface."""
    settings = websocket.app.state.settings
    if not _authorized(websocket, websocket.query_params.get("token"), settings.api_token):
        await websocket.close(code=1008, reason="bad or missing token")
        return

    await websocket.accept()
    hub: ChatHub = websocket.app.state.chat_hub
    history: ChatHistory = websocket.app.state.chat_history
    agent = getattr(websocket.app.state, "agent", None)
    hub.connect(websocket)
    log.info("web chat client connected (%d live)", hub.connected)
    try:
        await websocket.send_json({"type": "history", "messages": await history.recent()})
        while True:
            data = await websocket.receive_json()
            text = str(data.get("text", "")).strip()
            if not text:
                continue
            await history.append("web", "in", text)
            if agent is not None:
                agent.submit_message(
                    InboundMessage(surface="web", handle="user", text=text, chat_ref="web")
                )
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("web chat client errored")
    finally:
        hub.disconnect(websocket)
        log.info("web chat client left (%d live)", hub.connected)
