"""API route tests — the REST surface against a stub app.state."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI

from aether.agent.loop import CaptureRequestBox
from aether.api.routes import router
from aether.authz.approvals import Approval
from aether.config import Settings
from aether.memory.events import Event, IngestResult
from fakes import FakeApprovals, FakeAudit, FakeEventStore


class FakeScreenVision:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, str]] = []

    async def record(self, png: bytes, note: str = ""):
        self.calls.append((png, note))
        return IngestResult(stored=True, reason="new", event_id=1)


class FakeAgent:
    """The loop as the decide/notify surface needs it."""

    def __init__(self, decision_result: object = None) -> None:
        self.decision_result = decision_result
        self.calls: list[tuple[int, str]] = []
        self.notified = 0

    async def decide(self, approval_id: int, decision: str):
        self.calls.append((approval_id, decision))
        if isinstance(self.decision_result, Exception):
            raise self.decision_result
        return self.decision_result

    def notify(self) -> None:
        self.notified += 1


def _approval(approval_id: int = 1, status: str = "approved") -> Approval:
    now = datetime.now(timezone.utc)
    return Approval(
        id=approval_id, tool_name="mail__send_message",
        params={"to": "a@b.c"}, status=status, created_at=now,
        expires_at=now + timedelta(hours=24), decided_at=None, decided_by=None,
    )


def _event(event_id: int, kind: str = "chat_message") -> Event:
    return Event(
        id=event_id, source="telegram", kind=kind,
        occurred_at=datetime(2026, 9, 25, 10, event_id, tzinfo=timezone.utc),
        payload={"text": f"number {event_id}"}, salience_score=5.0,
        memorable=event_id == 2, meta={},
    )


def _app(
    vision=None,
    *,
    approvals: FakeApprovals | None = None,
    audit: FakeAudit | None = None,
    events: FakeEventStore | None = None,
    capture_box: CaptureRequestBox | None = None,
    agent: FakeAgent | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.settings = Settings(_env_file=None, api_token="secret")
    if vision is not None:
        app.state.screen_vision = vision
    if approvals is not None:
        app.state.approvals = approvals
    if audit is not None:
        app.state.audit = audit
    if events is not None:
        app.state.events = events
    if capture_box is not None:
        app.state.capture_box = capture_box
    if agent is not None:
        app.state.agent = agent
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_screen_capture_rejects_a_bad_token() -> None:
    vision = FakeScreenVision()
    async with _client(_app(vision)) as client:
        response = await client.post(
            "/api/screen-capture",
            params={"token": "wrong"},
            files={"file": ("s.png", b"png", "image/png")},
            data={"note": ""},
        )
    assert response.status_code == 401
    assert vision.calls == []


async def test_screen_capture_accepts_the_upload() -> None:
    vision = FakeScreenVision()
    async with _client(_app(vision)) as client:
        response = await client.post(
            "/api/screen-capture",
            params={"token": "secret"},
            files={"file": ("s.png", b"png-bytes", "image/png")},
            data={"note": "keep an eye on this"},
        )
    assert response.status_code == 202
    assert response.json() == {"stored": True, "reason": "new", "event_id": 1}
    assert vision.calls == [(b"png-bytes", "keep an eye on this")]


async def test_screen_capture_token_via_header() -> None:
    vision = FakeScreenVision()
    async with _client(_app(vision)) as client:
        response = await client.post(
            "/api/screen-capture",
            headers={"x-aether-token": "secret"},
            files={"file": ("s.png", b"png", "image/png")},
        )
    assert response.status_code == 202


async def test_screen_capture_without_vision_configured_is_503() -> None:
    async with _client(_app(None)) as client:  # no screen_vision on state
        response = await client.post(
            "/api/screen-capture",
            params={"token": "secret"},
            files={"file": ("s.png", b"png", "image/png")},
        )
    assert response.status_code == 503


async def test_screen_capture_rejects_an_empty_upload() -> None:
    vision = FakeScreenVision()
    async with _client(_app(vision)) as client:
        response = await client.post(
            "/api/screen-capture",
            params={"token": "secret"},
            files={"file": ("s.png", b"", "image/png")},
        )
    assert response.status_code == 400


async def test_screen_capture_wakes_the_agent() -> None:
    vision = FakeScreenVision()
    agent = FakeAgent()
    async with _client(_app(vision, agent=agent)) as client:
        response = await client.post(
            "/api/screen-capture",
            params={"token": "secret"},
            files={"file": ("s.png", b"png-bytes", "image/png")},
        )
    assert response.status_code == 202
    assert agent.notified == 1


# ---------------------------------------------------------------------------
# /api/screen-request — the companion's poll
# ---------------------------------------------------------------------------


async def test_screen_request_hands_over_one_capture() -> None:
    box = CaptureRequestBox()
    box.set("check the deploy log")
    async with _client(_app(None, capture_box=box)) as client:
        first = await client.get("/api/screen-request", params={"token": "secret"})
        second = await client.get("/api/screen-request", params={"token": "secret"})
    assert first.status_code == 200
    assert first.json() == {"note": "check the deploy log"}
    assert second.status_code == 204  # consumed — one capture per ask


async def test_screen_request_guards_and_degrades() -> None:
    box = CaptureRequestBox()
    async with _client(_app(None, capture_box=box)) as client:
        assert (await client.get("/api/screen-request")).status_code == 401
    async with _client(_app(None)) as client:  # no box wired
        response = await client.get("/api/screen-request", params={"token": "secret"})
    assert response.status_code == 503


# ---------------------------------------------------------------------------
# /api/approvals + decide — the web panel's buttons
# ---------------------------------------------------------------------------


async def test_approvals_feed_lists_pending_rows() -> None:
    approvals = FakeApprovals()
    async with _client(_app(None, approvals=approvals)) as client:
        assert (await client.get("/api/approvals")).status_code == 401
        empty = await client.get("/api/approvals", params={"token": "secret"})
        assert empty.json() == {"pending": []}

        await approvals.create(
            tool_name="mail__send_message", params={"to": "a@b.c"}, note="risky",
        )
        filled = await client.get("/api/approvals", params={"token": "secret"})
    pending = filled.json()["pending"]
    assert len(pending) == 1
    assert pending[0]["tool_name"] == "mail__send_message"
    assert pending[0]["params"] == {"to": "a@b.c"}
    assert pending[0]["created_at"].endswith("+00:00")  # ISO datetimes


async def test_decide_runs_a_fresh_approval() -> None:
    agent = FakeAgent(decision_result=_approval(3, status="approved"))
    async with _client(_app(None, agent=agent)) as client:
        response = await client.post(
            "/api/approvals/3/decide",
            params={"token": "secret"},
            json={"decision": "approve"},
        )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "status": "approved"}
    assert agent.calls == [(3, "approve")]


async def test_decide_reports_a_stale_one_without_running_it() -> None:
    agent = FakeAgent(decision_result=None)  # already decided or expired
    async with _client(_app(None, agent=agent)) as client:
        response = await client.post(
            "/api/approvals/9/decide",
            params={"token": "secret"},
            json={"decision": "deny"},
        )
    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "already decided or expired"}


async def test_decide_rejects_a_bad_decision_and_a_missing_agent() -> None:
    agent = FakeAgent(decision_result=ValueError("decision must be 'approve' or 'deny'"))
    async with _client(_app(None, agent=agent)) as client:
        response = await client.post(
            "/api/approvals/1/decide",
            params={"token": "secret"},
            json={"decision": "maybe"},
        )
    assert response.status_code == 400

    async with _client(_app(None)) as client:  # no agent on state
        response = await client.post(
            "/api/approvals/1/decide",
            params={"token": "secret"},
            json={"decision": "approve"},
        )
    assert response.status_code == 503


# ---------------------------------------------------------------------------
# /api/events + /api/audit — the feeds
# ---------------------------------------------------------------------------


async def test_events_feed_is_newest_first() -> None:
    store = FakeEventStore({1: _event(1), 2: _event(2), 3: _event(3, "poll:unread")})
    async with _client(_app(None, events=store)) as client:
        assert (await client.get("/api/events")).status_code == 401
        response = await client.get("/api/events", params={"token": "secret"})
    feed = response.json()["events"]
    assert [e["id"] for e in feed] == [3, 2, 1]
    assert feed[1]["memorable"] is True  # only event 2 was memorable
    assert feed[0]["kind"] == "poll:unread"
    assert feed[2]["payload"] == {"text": "number 1"}


async def test_audit_feed_carries_the_chain_verdict() -> None:
    audit = FakeAudit()
    await audit.append(
        actor="agent", tool_name="mail__list_messages", decision="allow",
        rules_matched="builtin:read-only", outcome="read-only tool",
    )
    async with _client(_app(None, audit=audit)) as client:
        assert (await client.get("/api/audit")).status_code == 401
        response = await client.get("/api/audit", params={"token": "secret"})
    data = response.json()
    assert data["chain"] == {"ok": True, "entries": 1, "first_bad_seq": None, "problem": None}
    entry = data["entries"][0]
    assert entry["seq"] == 1
    assert entry["tool"] == "mail__list_messages"
    assert entry["decision"] == "allow"
    assert entry["at"].endswith("+00:00")
