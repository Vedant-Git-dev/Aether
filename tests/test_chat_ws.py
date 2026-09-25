"""Web chat surface tests — token guard, history replay, the inbound path
to the agent loop, and hub broadcast — over a FastAPI app with fakes on
state (hermetic: no database, no agent).

The TestClient runs the app on its own event loop (portal thread), so the
fake agent echoes via a task on that same loop — exactly how a live agent's
reply would land on the socket.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from aether.chat.ws import ChatHub, router as chat_router
from aether.config import Settings
from aether.connectors.base import InboundMessage


class FakeHistory:
    def __init__(self, rows: list[tuple[str, str, str]] | None = None) -> None:
        self.rows = list(rows or [])

    async def append(self, surface: str, direction: str, text: str) -> None:
        self.rows.append((surface, direction, text))

    async def recent(self, limit: int = 50) -> list[dict]:
        return [
            {"direction": d, "text": t, "at": "2026-09-25T00:00:00+00:00"}
            for _, d, t in self.rows[-limit:]
        ]


class FakeAgent:
    """Records submissions and echoes back over the hub, like a live loop."""

    def __init__(self, hub: ChatHub) -> None:
        self.hub = hub
        self.submitted: list[InboundMessage] = []

    def submit_message(self, message: InboundMessage) -> None:
        self.submitted.append(message)
        asyncio.get_running_loop().create_task(self.hub.broadcast(f"echo: {message.text}"))


def _app(agent=None, history: FakeHistory | None = None):
    history = history or FakeHistory()
    app = FastAPI()
    app.include_router(chat_router)
    app.state.settings = Settings(_env_file=None, api_token="secret")
    app.state.chat_history = history
    hub = ChatHub(history)
    app.state.chat_hub = hub
    app.state.agent = agent

    @app.get("/_broadcast")
    async def _broadcast(text: str = "from the loop"):
        """Test hook: drive an async hub broadcast from a sync test."""
        await hub.broadcast(text)
        return {"ok": True}

    return app, hub, history


def test_a_bad_token_is_refused_before_the_socket_opens() -> None:
    app, _, _ = _app()
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws?token=wrong"):
            pass


def test_a_missing_token_is_refused_too() -> None:
    app, _, _ = _app()
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass


def test_history_replays_then_chat_flows_both_ways() -> None:
    history = FakeHistory([("web", "in", "earlier question"),
                           ("web", "out", "earlier answer")])
    app, hub, history = _app(history=history)
    agent = FakeAgent(hub)
    app.state.agent = agent

    with TestClient(app).websocket_connect("/ws?token=secret") as ws:
        first = ws.receive_json()
        assert first["type"] == "history"
        assert [m["text"] for m in first["messages"]] == \
            ["earlier question", "earlier answer"]

        ws.send_json({"text": "what do you remember?"})
        reply = ws.receive_json()
        assert reply == {"type": "chat", "text": "echo: what do you remember?"}

    # the inbound line reached the agent like any other surface would
    assert len(agent.submitted) == 1
    message = agent.submitted[0]
    assert (message.surface, message.handle, message.text) == \
        ("web", "user", "what do you remember?")
    # and both directions were persisted to the transcript
    assert ("web", "in", "what do you remember?") in history.rows
    assert ("web", "out", "echo: what do you remember?") in history.rows


def test_broadcast_reaches_every_connected_client() -> None:
    app, hub, history = _app()
    client = TestClient(app)
    with client.websocket_connect("/ws?token=secret") as ws1, \
         client.websocket_connect("/ws?token=secret") as ws2:
        for ws in (ws1, ws2):
            ws.receive_json()  # history
        client.get("/_broadcast?text=good%20morning")
        assert ws1.receive_json() == {"type": "chat", "text": "good morning"}
        assert ws2.receive_json() == {"type": "chat", "text": "good morning"}
    assert ("web", "out", "good morning") in history.rows


def test_blank_lines_are_ignored() -> None:
    app, hub, _ = _app()
    agent = FakeAgent(hub)
    app.state.agent = agent
    with TestClient(app).websocket_connect("/ws?token=secret") as ws:
        ws.receive_json()  # history
        ws.send_json({"text": "   "})
        # nothing comes back and nothing was submitted; a second real line works
        ws.send_json({"text": "hello"})
        assert ws.receive_json() == {"type": "chat", "text": "echo: hello"}
    assert [m.text for m in agent.submitted] == ["hello"]


# ---------------------------------------------------------------------------
# integration: the real encrypted transcript
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_chat_history_round_trips_encrypted(db) -> None:
    from aether.chat.ws import ChatHistory
    from aether.memory.crypto import Cipher, generate_key_b64

    history = ChatHistory(db, Cipher.from_b64(generate_key_b64()))
    await history.append("web", "in", "hello there")
    await history.append("web", "out", "hi right back")

    rows = await history.recent()
    assert [r["direction"] for r in rows] == ["in", "out"]
    assert [r["text"] for r in rows] == ["hello there", "hi right back"]

    # on disk: ciphertext
    raw = await db.fetchval("SELECT payload_enc FROM chat_messages WHERE id = 1")
    assert b"hello there" not in raw

    # a row this key can't decrypt is skipped, not fatal
    await db.execute("UPDATE chat_messages SET payload_enc = $1 WHERE id = 2", b"garbage")
    assert [r["text"] for r in await history.recent()] == ["hello there"]
