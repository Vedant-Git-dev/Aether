"""A real Aether web app wired to in-memory fakes instead of Postgres.

This is what the E2E tests drive with a real browser, and what a developer
can run by hand to click through the frontend without setting up a database:

    python -m tests.e2e.dev_server

Every fake here implements exactly the surface the real store classes do
(same methods, same return shapes) — routes.py and the frontend can't tell
the difference. Not a mock of the HTTP layer: the real FastAPI app, the
real routers, the real static files, just a fake persistence layer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from aether.api import router as api_router
from aether.authz.approvals import PENDING, Approval
from aether.chat.ws import router as chat_router
from aether.config import (
    AppConfig,
    AuthzConfig,
    AuthzRule,
    MCPServerConfig,
    MessagingConfig,
    PlatformToggle,
    Settings,
    TransportConfig,
)
from aether.connectors.base import InboundMessage
from aether.memory.entities import Entity, EntityNote
from aether.memory.events import Event
from aether.scheduler.jobs import ScheduledAction

WEB_DIR = Path(__file__).resolve().parents[2] / "src" / "aether" / "web"
API_TOKEN = "secret"


class FakeApprovals:
    def __init__(self, now: datetime) -> None:
        self._rows = {
            1: Approval(
                id=1,
                tool_name="mail__send_message",
                params={
                    "to": "sam@example.com",
                    "subject": "Re: Thursday",
                    "body": "Works for me.",
                },
                status=PENDING,
                created_at=now - timedelta(minutes=6),
                expires_at=now + timedelta(hours=24),
                decided_at=None,
                decided_by=None,
            ),
            2: Approval(
                id=2,
                tool_name="telegram__send_message",
                params={"chat_ref": "@sam", "text": "confirmed for 4pm"},
                status=PENDING,
                created_at=now - timedelta(minutes=2),
                expires_at=now + timedelta(hours=24),
                decided_at=None,
                decided_by=None,
            ),
        }

    async def list_pending(self) -> list[Approval]:
        return [a for a in self._rows.values() if a.status == PENDING]

    async def decide(self, approval_id: int, decision: str) -> Approval | None:
        row = self._rows.get(approval_id)
        if row is None or row.status != PENDING:
            return None
        status = "approved" if decision == "approve" else "denied"
        updated = Approval(
            id=row.id,
            tool_name=row.tool_name,
            params=row.params,
            status=status,
            created_at=row.created_at,
            expires_at=row.expires_at,
            decided_at=datetime.now(UTC),
            decided_by="web",
        )
        self._rows[approval_id] = updated
        return updated


class FakeAgent:
    def __init__(self, approvals: FakeApprovals) -> None:
        self._approvals = approvals

    async def decide(self, approval_id: int, decision: str) -> Approval | None:
        return await self._approvals.decide(approval_id, decision)

    def notify(self) -> None:
        pass

    def submit_message(self, msg: InboundMessage) -> None:
        pass


class FakeAudit:
    def __init__(self, now: datetime) -> None:
        self.rows = [
            {
                "seq": 1,
                "actor": "agent",
                "tool_name": "mail__list_messages",
                "decision": "allow",
                "rules_matched": "builtin:read-only",
                "outcome": "read-only tool",
                "created_at": now - timedelta(hours=2),
                "entry_hash": "7e4c19a04f8e2d1b91a0c3f5d8e7b2a6c4d9f1e8b3a7c2d5e9f0a1b4c8d6e3f7",
            },
            {
                "seq": 2,
                "actor": "agent",
                "tool_name": "telegram__send_message",
                "decision": "approve",
                "rules_matched": "builtin:risky",
                "outcome": "parked for approval",
                "created_at": now - timedelta(hours=1),
                "entry_hash": "b8e4d2f19a3c7e0b5d8f2a6c9e1b4d7f0a3c6e9b2d5f8a1c4e7b0d3f6a9c2e5b",
            },
        ]

    async def recent(self, limit: int = 100) -> list[dict]:
        return list(reversed(self.rows))[:limit]  # newest first, matching the real AuditLog

    async def verify_chain(self):
        from aether.authz.audit import ChainVerification

        return ChainVerification(ok=True, entries=len(self.rows), first_bad_seq=None, problem=None)


class FakeEventStore:
    def __init__(self, now: datetime) -> None:
        self.rows = [
            Event(
                id=2,
                source="telegram",
                kind="chat_message",
                occurred_at=now - timedelta(minutes=1),
                payload={"text": "confirmed for 4pm"},
                salience_score=8.0,
                memorable=True,
                meta={},
            ),
            Event(
                id=1,
                source="mail",
                kind="poll:unread",
                occurred_at=now - timedelta(minutes=10),
                payload={"from": "sam@example.com", "subject": "Re: Thursday"},
                salience_score=7.0,
                memorable=True,
                meta={},
            ),
        ]

    async def recent(self, limit: int = 50) -> list[Event]:
        return self.rows[:limit]


class FakeChatHistory:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def append(self, surface: str, direction: str, text: str) -> None:
        self.rows.append(
            {"direction": direction, "text": text, "at": datetime.now(UTC).isoformat()}
        )

    async def recent(self, limit: int = 50) -> list[dict]:
        return self.rows[-limit:]


class FakeChatHub:
    def __init__(self, history: FakeChatHistory) -> None:
        self._history = history
        self._clients: set[WebSocket] = set()

    @property
    def connected(self) -> int:
        return len(self._clients)

    def connect(self, ws: WebSocket) -> None:
        self._clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def broadcast(self, text: str, *, persist: bool = True) -> None:
        if persist:
            await self._history.append("web", "out", text)
        for ws in list(self._clients):
            try:
                await ws.send_json({"type": "chat", "text": text})
            except Exception:
                self.disconnect(ws)


class FakeAgentSettings:
    def __init__(self) -> None:
        self.personality = ""

    async def get_personality(self) -> str:
        return self.personality

    async def set_personality(self, text: str) -> str:
        self.personality = text.strip()
        return self.personality


class FakeEntities:
    def __init__(self, now: datetime) -> None:
        self.people = [
            Entity(id=1, display_name="Sam", confidence=0.94, handles=[("telegram", "@sam")]),
        ]
        self.notes = {
            1: [
                EntityNote(
                    id=1,
                    identity_id=1,
                    created_at=now - timedelta(hours=2),
                    payload={"kind": "relationship", "text": "coworker on the infra team"},
                )
            ],
        }

    async def list_people(self, limit: int = 50) -> list[Entity]:
        return self.people[:limit]

    async def notes_for(self, identity_id: int, limit: int = 10) -> list[EntityNote]:
        return self.notes.get(identity_id, [])[:limit]


class FakeScheduler:
    def __init__(self, now: datetime) -> None:
        self.actions = [
            ScheduledAction(
                id=1,
                label="follow up with sam re: thursday",
                run_at=now + timedelta(hours=3),
                status="pending",
                payload={"type": "tool", "tool": "mail__send_message", "params": {}},
                created_at=now - timedelta(minutes=30),
            ),
        ]

    async def list(self, limit: int = 50) -> list[ScheduledAction]:
        return self.actions[:limit]


def build_app() -> FastAPI:
    """A fresh app + fresh in-memory state — call once per test for isolation."""
    now = datetime.now(UTC)
    app = FastAPI(title="Aether (e2e dev fixture)")
    app.include_router(api_router)
    app.include_router(chat_router)
    app.mount("/assets", StaticFiles(directory=WEB_DIR), name="web-assets")

    app.state.settings = Settings(_env_file=None, api_token=API_TOKEN)
    app.state.config = AppConfig(
        authz=AuthzConfig(
            rules=[
                AuthzRule(
                    tool_pattern="telegram__send_message", decision="approve", note="one-tap sends"
                ),
            ],
            approval_ttl_hours=24,
        ),
        messaging=MessagingConfig(
            telegram=PlatformToggle(enabled=True),
            discord=PlatformToggle(enabled=False),
            slack=PlatformToggle(enabled=False),
        ),
        mcp_servers=[
            MCPServerConfig(name="mail", transport=TransportConfig(type="http", url="https://x"))
        ],
    )
    approvals = FakeApprovals(now)
    app.state.approvals = approvals
    app.state.audit = FakeAudit(now)
    app.state.events = FakeEventStore(now)
    app.state.agent = FakeAgent(approvals)
    chat_history = FakeChatHistory()
    app.state.chat_history = chat_history
    app.state.chat_hub = FakeChatHub(chat_history)
    app.state.agent_settings = FakeAgentSettings()
    app.state.entities = FakeEntities(now)
    app.state.scheduler = FakeScheduler(now)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(build_app(), host="127.0.0.1", port=8731, log_level="info")
