"""API route tests — the REST surface against a stub app.state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from fakes import (
    FakeAgentSettings,
    FakeApprovals,
    FakeAudit,
    FakeConfigManager,
    FakeEventStore,
)
from fastapi import FastAPI

from aether.agent.loop import CaptureRequestBox
from aether.api.routes import router
from aether.authz.approvals import Approval
from aether.config import AppConfig, AuthzConfig, AuthzRule, Settings
from aether.memory.crypto import generate_key_b64
from aether.memory.entities import Entity, EntityNote
from aether.memory.events import Event, IngestResult
from aether.oauth import OAuthError, OAuthFlow, TokenSet
from aether.scheduler.jobs import ScheduledAction


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
    now = datetime.now(UTC)
    return Approval(
        id=approval_id,
        tool_name="mail__send_message",
        params={"to": "a@b.c"},
        status=status,
        created_at=now,
        expires_at=now + timedelta(hours=24),
        decided_at=None,
        decided_by=None,
    )


def _event(event_id: int, kind: str = "chat_message") -> Event:
    return Event(
        id=event_id,
        source="telegram",
        kind=kind,
        occurred_at=datetime(2026, 9, 25, 10, event_id, tzinfo=UTC),
        payload={"text": f"number {event_id}"},
        salience_score=5.0,
        memorable=event_id == 2,
        meta={},
    )


def _app(
    vision=None,
    *,
    approvals: FakeApprovals | None = None,
    audit: FakeAudit | None = None,
    events: FakeEventStore | None = None,
    capture_box: CaptureRequestBox | None = None,
    agent: FakeAgent | None = None,
    config: AppConfig | None = None,
    agent_settings: FakeAgentSettings | None = None,
    config_manager: FakeConfigManager | None = None,
    entities=None,
    scheduler=None,
    settings: Settings | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.settings = settings or Settings(_env_file=None, api_token="secret")
    app.state.config = config or AppConfig()
    if entities is not None:
        app.state.entities = entities
    if scheduler is not None:
        app.state.scheduler = scheduler
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
    if agent_settings is not None:
        app.state.agent_settings = agent_settings
    if config_manager is not None:
        app.state.config_manager = config_manager
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
            tool_name="mail__send_message",
            params={"to": "a@b.c"},
            note="risky",
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
        actor="agent",
        tool_name="mail__list_messages",
        decision="allow",
        rules_matched="builtin:read-only",
        outcome="read-only tool",
    )
    async with _client(_app(None, audit=audit)) as client:
        assert (await client.get("/api/audit")).status_code == 401
        response = await client.get("/api/audit", params={"token": "secret"})
    data = response.json()
    assert data["chain"] == {
        "ok": True, "entries": 1, "first_bad_seq": None, "problem": None, "head_hash": "fake-hash-1",
    }
    entry = data["entries"][0]
    assert entry["seq"] == 1
    assert entry["hash"] == "fake-hash-1"
    assert entry["tool"] == "mail__list_messages"
    assert entry["decision"] == "allow"
    assert entry["at"].endswith("+00:00")


# ---------------------------------------------------------------------------
# /api/policy — the read-only view of config.yaml's authz rules
# ---------------------------------------------------------------------------


async def test_policy_feed_is_token_guarded() -> None:
    async with _client(_app(None)) as client:
        response = await client.get("/api/policy")
    assert response.status_code == 401


async def test_policy_feed_reports_configured_rules_and_the_builtin_ladder() -> None:
    config = AppConfig(
        authz=AuthzConfig(
            rules=[
                AuthzRule(
                    tool_pattern="telegram__send_message",
                    decision="approve",
                    note="telegram sends are one-tap",
                ),
            ],
            approval_ttl_hours=12,
        )
    )
    async with _client(_app(None, config=config)) as client:
        response = await client.get("/api/policy", params={"token": "secret"})
    data = response.json()
    assert data["approval_ttl_hours"] == 12
    assert data["rules"] == [
        {
            "tool_pattern": "telegram__send_message",
            "param_pattern": None,
            "decision": "approve",
            "note": "telegram sends are one-tap",
        }
    ]
    assert len(data["builtin_rules"]) == 6
    by_id = {r["id"]: r for r in data["builtin_rules"]}
    assert set(by_id) == {
        "builtin:config-tune",
        "builtin:config-security",
        "builtin:internal",
        "builtin:risky",
        "builtin:read-only",
        "default:fail-safe",
    }
    # the chat-configuration gate, as the panel explains it
    assert by_id["builtin:config-tune"]["decision"] == "allow"
    assert by_id["builtin:config-security"]["decision"] == "require_approval"


# ---------------------------------------------------------------------------
# /api/config — the read-only live config + chat-made overrides
# ---------------------------------------------------------------------------


async def test_config_feed_is_token_guarded() -> None:
    async with _client(_app(None, config_manager=FakeConfigManager())) as client:
        assert (await client.get("/api/config")).status_code == 401


async def test_config_feed_is_503_without_a_manager_wired() -> None:
    async with _client(_app(None)) as client:  # no config_manager on state
        response = await client.get("/api/config", params={"token": "secret"})
    assert response.status_code == 503


async def test_config_feed_serves_the_effective_shape() -> None:
    payload = {
        "sections": {"agent": {"tick_seconds": 10.0}},
        "overrides": [{"path": "agent.tick_seconds", "value": 10.0}],
    }
    manager = FakeConfigManager(effective_payload=payload)
    async with _client(_app(None, config_manager=manager)) as client:
        response = await client.get("/api/config", params={"token": "secret"})
    assert response.status_code == 200
    assert response.json() == payload


# ---------------------------------------------------------------------------
# /api/settings/personality — the owner's custom behavior instructions
# ---------------------------------------------------------------------------


async def test_personality_feed_is_token_guarded() -> None:
    async with _client(_app(None, agent_settings=FakeAgentSettings())) as client:
        assert (await client.get("/api/settings/personality")).status_code == 401
        assert (
            await client.put("/api/settings/personality", json={"text": "x"})
        ).status_code == 401


async def test_personality_feed_is_503_without_a_store_wired() -> None:
    async with _client(_app(None)) as client:  # no agent_settings on state
        response = await client.get("/api/settings/personality", params={"token": "secret"})
    assert response.status_code == 503


async def test_personality_round_trips_through_get_and_put() -> None:
    store = FakeAgentSettings()
    async with _client(_app(None, agent_settings=store)) as client:
        empty = await client.get("/api/settings/personality", params={"token": "secret"})
        assert empty.json() == {"text": ""}

        saved = await client.put(
            "/api/settings/personality",
            params={"token": "secret"},
            json={"text": "  Be warm and a little informal.  "},
        )
        assert saved.json() == {"ok": True, "text": "Be warm and a little informal."}

        fetched = await client.get("/api/settings/personality", params={"token": "secret"})
    assert fetched.json() == {"text": "Be warm and a little informal."}
    assert store.personality == "Be warm and a little informal."


# ---------------------------------------------------------------------------
# /api/settings/apps — real configured connectors + capabilities
# ---------------------------------------------------------------------------


async def test_apps_feed_is_token_guarded() -> None:
    async with _client(_app(None)) as client:
        assert (await client.get("/api/settings/apps")).status_code == 401


async def test_apps_feed_reports_configured_connectors_and_capabilities() -> None:
    from aether.config import (
        AppConfig,
        ContactsConfig,
        MCPServerConfig,
        MessagingConfig,
        PlatformToggle,
        TransportConfig,
    )

    config = AppConfig(
        messaging=MessagingConfig(
            telegram=PlatformToggle(enabled=True),
            discord=PlatformToggle(enabled=False),
            slack=PlatformToggle(enabled=False),
        ),
        mcp_servers=[
            MCPServerConfig(name="mail", transport=TransportConfig(type="http", url="https://x")),
        ],
        contacts=ContactsConfig(mode="enforce"),
    )
    async with _client(_app(config=config, vision=FakeScreenVision())) as client:
        response = await client.get("/api/settings/apps", params={"token": "secret"})
    data = response.json()
    assert {"name": "telegram", "enabled": True} in data["connectors"]
    assert {"name": "discord", "enabled": False} in data["connectors"]
    assert data["mcp_servers"] == [{"name": "mail", "transport": "http", "enabled": True}]
    assert data["screen_vision_available"] is True
    assert data["contacts_mode"] == "enforce"


async def test_apps_feed_reports_screen_vision_unavailable_without_a_provider() -> None:
    async with _client(_app(None)) as client:  # no screen_vision on state
        response = await client.get("/api/settings/apps", params={"token": "secret"})
    assert response.json()["screen_vision_available"] is False


# ---------------------------------------------------------------------------
# /api/memory — resolved cross-platform identities + their latest note
# ---------------------------------------------------------------------------


class FakeEntitiesStore:
    def __init__(
        self, people: list[Entity] | None = None, notes: dict[int, list[EntityNote]] | None = None
    ):
        self.people = people or []
        self.notes = notes or {}

    async def list_people(self, limit: int = 50) -> list[Entity]:
        return self.people[:limit]

    async def notes_for(self, identity_id: int, limit: int = 10) -> list[EntityNote]:
        return self.notes.get(identity_id, [])[:limit]


async def test_memory_feed_is_token_guarded() -> None:
    async with _client(_app(None, entities=FakeEntitiesStore())) as client:
        assert (await client.get("/api/memory")).status_code == 401


async def test_memory_feed_is_503_without_a_store_wired() -> None:
    async with _client(_app(None)) as client:  # no entities on state
        response = await client.get("/api/memory", params={"token": "secret"})
    assert response.status_code == 503


async def test_memory_feed_reports_people_handles_and_latest_note() -> None:
    person = Entity(id=1, display_name="Sam", confidence=0.92, handles=[("telegram", "@sam")])
    note = EntityNote(
        id=9,
        identity_id=1,
        created_at=datetime(2026, 9, 20, tzinfo=UTC),
        payload={"kind": "relationship", "text": "coworker on the infra team"},
    )
    store = FakeEntitiesStore(people=[person], notes={1: [note]})
    async with _client(_app(None, entities=store)) as client:
        response = await client.get("/api/memory", params={"token": "secret"})
    data = response.json()
    assert data["people"] == [
        {
            "id": 1,
            "display_name": "Sam",
            "confidence": 0.92,
            "handles": [{"platform": "telegram", "handle": "@sam"}],
            "latest_note": {
                "at": "2026-09-20T00:00:00+00:00",
                "payload": {"kind": "relationship", "text": "coworker on the infra team"},
            },
        }
    ]


async def test_memory_feed_reports_null_note_for_a_person_with_none() -> None:
    person = Entity(id=2, display_name="Unnoted", confidence=1.0, handles=[])
    store = FakeEntitiesStore(people=[person])
    async with _client(_app(None, entities=store)) as client:
        response = await client.get("/api/memory", params={"token": "secret"})
    assert response.json()["people"][0]["latest_note"] is None


# ---------------------------------------------------------------------------
# /api/tasks — persisted scheduled actions
# ---------------------------------------------------------------------------


class FakeSchedulerStore:
    def __init__(self, actions: list[ScheduledAction] | None = None):
        self.actions = actions or []

    async def list(self, limit: int = 50) -> list[ScheduledAction]:
        return self.actions[:limit]


async def test_tasks_feed_is_token_guarded() -> None:
    async with _client(_app(None, scheduler=FakeSchedulerStore())) as client:
        assert (await client.get("/api/tasks")).status_code == 401


async def test_tasks_feed_is_503_without_a_scheduler_wired() -> None:
    async with _client(_app(None)) as client:  # no scheduler on state
        response = await client.get("/api/tasks", params={"token": "secret"})
    assert response.status_code == 503


async def test_tasks_feed_reports_scheduled_actions() -> None:
    action = ScheduledAction(
        id=5,
        label="follow up with sam",
        run_at=datetime(2026, 9, 27, 9, tzinfo=UTC),
        status="pending",
        payload={"type": "tool", "tool": "mail__send_message", "params": {}},
        created_at=datetime(2026, 9, 26, tzinfo=UTC),
    )
    store = FakeSchedulerStore(actions=[action])
    async with _client(_app(None, scheduler=store)) as client:
        response = await client.get("/api/tasks", params={"token": "secret"})
    assert response.json()["tasks"] == [
        {
            "id": 5,
            "label": "follow up with sam",
            "run_at": "2026-09-27T09:00:00+00:00",
            "status": "pending",
            "payload": {"type": "tool", "tool": "mail__send_message", "params": {}},
            "created_at": "2026-09-26T00:00:00+00:00",
        }
    ]


# ---------------------------------------------------------------------------
# /oauth/callback — the public landing of a Google sign-in
# ---------------------------------------------------------------------------

_KEY = generate_key_b64()  # the deployment's permanent key, as boot would have


class FakeExchange:
    """exchange_code double: records the trade, answers canned (or refuses)."""

    def __init__(self, tokens: TokenSet | None = None, error: OAuthError | None = None) -> None:
        self.tokens = tokens
        self.error = error
        self.calls: list[dict] = []

    async def __call__(self, provider, *, code, client_id, client_secret, redirect):
        self.calls.append({
            "provider": provider, "code": code, "client_id": client_id,
            "client_secret": client_secret, "redirect": redirect,
        })
        if self.error is not None:
            raise self.error
        return self.tokens


class FakeOAuthStore:
    def __init__(self) -> None:
        self.saved: list[tuple[str, TokenSet]] = []

    async def save(self, provider: str, tokens: TokenSet) -> None:
        self.saved.append((provider, tokens))


class FakeResolver:
    def __init__(self, **values: str) -> None:
        self.values = values

    async def resolve(self, name: str) -> str | None:
        return self.values.get(name)


class FakeFinisher:
    """The loop's finish_oauth double — the card it sends is loop-side."""

    def __init__(self) -> None:
        self.finished: list[tuple[str, str]] = []

    async def finish_oauth(self, provider: str, app_key: str) -> None:
        self.finished.append((provider, app_key))


def _token_set() -> TokenSet:
    return TokenSet(
        access_token="ya29.a", refresh_token="1//r", scopes="gmail.modify",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def _oauth_app(
    *, resolver: FakeResolver | None = None,
    store: FakeOAuthStore | None = None,
    finisher: FakeFinisher | None = None,
    settings: Settings | None = None,
) -> tuple[FastAPI, FakeOAuthStore, FakeFinisher | None]:
    app = FastAPI()
    app.include_router(router)
    app.state.settings = settings or Settings(
        _env_file=None, api_token="secret", encryption_key=_KEY
    )
    store = store or FakeOAuthStore()
    app.state.oauth_store = store
    if resolver is not None:
        app.state.resolver = resolver
    if finisher is not None:
        app.state.agent = finisher
    return app, store, finisher


def _state(expiry: float = 9999999999.0) -> str:
    return OAuthFlow(_KEY)._sign("google", "gmail", "https://r.example/cb", expiry)


async def test_the_callback_is_public_and_finishes_the_sign_in(monkeypatch) -> None:
    exchange = FakeExchange(tokens=_token_set())
    monkeypatch.setattr("aether.api.routes.exchange_code", exchange)
    app, store, finisher = _oauth_app(finisher=FakeFinisher())
    async with _client(app) as client:  # no token anywhere — the link is the proof
        response = await client.get(
            "/oauth/callback", params={"code": "4/0Abc", "state": _state()}
        )
    assert response.status_code == 200
    assert "authorized" in response.text
    # the exchange replays the redirect from the signed state, exactly
    (call,) = exchange.calls
    assert call["provider"] == "google"
    assert call["code"] == "4/0Abc"
    assert call["redirect"] == "https://r.example/cb"
    assert [provider for provider, _ in store.saved] == ["google"]
    assert finisher is not None
    assert finisher.finished == [("google", "gmail")]  # chat continues by itself


async def test_the_callback_reads_client_keys_through_the_resolver(monkeypatch) -> None:
    exchange = FakeExchange(tokens=_token_set())
    monkeypatch.setattr("aether.api.routes.exchange_code", exchange)
    resolver = FakeResolver(GOOGLE_CLIENT_ID="cid-fresh", GOOGLE_CLIENT_SECRET="cs-fresh")
    app, _, _ = _oauth_app(resolver=resolver)
    async with _client(app) as client:
        await client.get("/oauth/callback", params={"code": "4/x", "state": _state()})
    (call,) = exchange.calls
    assert call["client_id"] == "cid-fresh"
    assert call["client_secret"] == "cs-fresh"


async def test_a_state_from_somewhere_else_is_refused_plainly(monkeypatch) -> None:
    exchange = FakeExchange(tokens=_token_set())
    monkeypatch.setattr("aether.api.routes.exchange_code", exchange)
    app, store, _ = _oauth_app()
    stranger = OAuthFlow(generate_key_b64())._sign(
        "google", "gmail", "https://r.example/cb", 9999999999.0
    )
    async with _client(app) as client:
        response = await client.get(
            "/oauth/callback", params={"code": "4/x", "state": stranger}
        )
    assert response.status_code == 400
    assert "fresh one" in response.text  # the honest ask: start again in chat
    assert exchange.calls == []
    assert store.saved == []


async def test_the_provider_refusal_stores_nothing(monkeypatch) -> None:
    exchange = FakeExchange(error=OAuthError("the provider refused: invalid_grant"))
    monkeypatch.setattr("aether.api.routes.exchange_code", exchange)
    app, store, finisher = _oauth_app(finisher=FakeFinisher())
    async with _client(app) as client:
        response = await client.get(
            "/oauth/callback", params={"code": "4/x", "state": _state()}
        )
    assert response.status_code == 400
    assert "nothing was stored" in response.text
    assert "invalid_grant" in response.text
    assert store.saved == []
    assert finisher is not None and finisher.finished == []


async def test_a_refused_or_incomplete_sign_in_gets_a_polite_page() -> None:
    app, store, _ = _oauth_app()
    async with _client(app) as client:
        refused = await client.get("/oauth/callback", params={"error": "access_denied"})
        empty = await client.get("/oauth/callback")
    assert refused.status_code == 200  # the tab still closes politely
    assert empty.status_code == 200
    assert "Nothing was received" in refused.text
    assert store.saved == []


async def test_no_permanent_key_refuses_to_store_tokens(monkeypatch) -> None:
    exchange = FakeExchange(tokens=_token_set())
    monkeypatch.setattr("aether.api.routes.exchange_code", exchange)
    settings = Settings(_env_file=None, api_token="secret")  # no encryption_key
    app, store, _ = _oauth_app(settings=settings)
    async with _client(app) as client:
        response = await client.get(
            "/oauth/callback", params={"code": "4/x", "state": _state()}
        )
    assert response.status_code == 400
    assert "AETHER_ENCRYPTION_KEY" in response.text
    assert exchange.calls == []
    assert store.saved == []
