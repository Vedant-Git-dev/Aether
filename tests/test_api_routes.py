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
    FakeProactiveStore,
    FakeTraces,
)
from fastapi import FastAPI

from aether.agent.loop import CaptureRequestBox
from aether.api.routes import router
from aether.authz.approvals import Approval
from aether.config import (
    AppConfig,
    AuthzConfig,
    AuthzRule,
    ContactRule,
    ContactsConfig,
    MessagingConfig,
    PlatformToggle,
    Settings,
)
from aether.memory.entities import Entity, EntityNote
from aether.memory.events import Event, IngestResult
from aether.scheduler.jobs import ScheduledAction
from aether.workspace import Workspace


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
        self.submitted: list = []

    async def decide(self, approval_id: int, decision: str):
        self.calls.append((approval_id, decision))
        if isinstance(self.decision_result, Exception):
            raise self.decision_result
        return self.decision_result

    def notify(self) -> None:
        self.notified += 1

    def submit_message(self, message) -> None:
        self.submitted.append(message)


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
    provider_runtime=None,
    config_manager: FakeConfigManager | None = None,
    entities=None,
    scheduler=None,
    workspace=None,
    transfers=None,
    settings: Settings | None = None,
    traces: FakeTraces | None = None,
    proactive_store=None,
    proactive_worker=None,
    media_store=None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.settings = settings or Settings.without_env_file(api_token="secret")
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
    if provider_runtime is not None:
        app.state.provider_runtime = provider_runtime
    if config_manager is not None:
        app.state.config_manager = config_manager
    if workspace is not None:
        app.state.workspace = workspace
    if transfers is not None:
        app.state.transfers = transfers
    if traces is not None:
        app.state.traces = traces
    if proactive_store is not None:
        app.state.proactive_store = proactive_store
    if proactive_worker is not None:
        app.state.proactive_worker = proactive_worker
    if media_store is not None:
        app.state.media_store = media_store
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
            rules_matched="builtin:risky",
            note="reaches an external system or is hard to undo",
        )
        filled = await client.get("/api/approvals", params={"token": "secret"})
    pending = filled.json()["pending"]
    assert len(pending) == 1
    assert pending[0]["tool_name"] == "mail__send_message"
    assert pending[0]["params"] == {"to": "a@b.c"}
    # why the gate parked it — the card's plain-language reason
    assert pending[0]["rules_matched"] == "builtin:risky"
    assert pending[0]["note"] == "reaches an external system or is hard to undo"
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
        "ok": True,
        "entries": 1,
        "first_bad_seq": None,
        "problem": None,
        "head_hash": "fake-hash-1",
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
# /api/contacts — the panel's one config edit surface
# ---------------------------------------------------------------------------


def _contacts_config() -> AppConfig:
    return AppConfig(
        contacts=ContactsConfig(
            mode="enforce",
            allowlist=[ContactRule(platform="telegram", handle="@sam")],
        )
    )


async def test_contacts_feed_is_token_guarded() -> None:
    async with _client(_app(None, config=_contacts_config())) as client:
        assert (await client.get("/api/contacts")).status_code == 401


async def test_contacts_feed_serves_the_live_list() -> None:
    async with _client(_app(None, config=_contacts_config())) as client:
        response = await client.get("/api/contacts", params={"token": "secret"})
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "enforce"
    assert body["allowlist"] == [{"platform": "telegram", "handle": "@sam"}]
    # no app is connected and none carries its own list — no cards, the
    # cross-app list and the default carry everything
    assert body["apps"] == []


async def test_contacts_feed_lists_apps_with_their_own_slices() -> None:
    from aether.config import AppContacts

    config = AppConfig(
        messaging=MessagingConfig(telegram=PlatformToggle(enabled=True)),
        contacts=ContactsConfig(
            mode="enforce",
            allowlist=[ContactRule(platform="telegram", handle="@sam")],
            gmail=AppContacts(
                allowlist=[ContactRule(platform="gmail", handle="friend@example.com")]
            ),
        ),
    )
    async with _client(_app(None, config=config)) as client:
        body = (await client.get("/api/contacts", params={"token": "secret"})).json()
    apps = {a["key"]: a for a in body["apps"]}
    # telegram: connected via its toggle; its card merges the same-platform
    # stray from the cross-app bucket, tagged with the list it lives in
    assert apps["telegram"]["connected"] is True
    assert apps["telegram"]["mode"] is None  # follows the default
    assert apps["telegram"]["allowlist"] == [
        {"platform": "telegram", "handle": "@sam", "list": "any"}
    ]
    # gmail: not connected, but its own list shows it — marked honestly
    assert apps["gmail"]["connected"] is False
    assert apps["gmail"]["allowlist"] == [
        {"platform": "gmail", "handle": "friend@example.com", "list": "app"}
    ]
    # discord: nothing connected, nothing configured — no card
    assert "discord" not in apps


async def test_contacts_feed_lists_a_connected_direct_provider_app() -> None:
    class _ProviderFeed:
        async def feed(self):
            return {
                "apps": [
                    {
                        "key": "gmail",
                        "name": "Gmail",
                        "status": "active",
                        "identity": "me@example.com",
                    }
                ]
            }

    async with _client(
        _app(None, config=_contacts_config(), provider_runtime=_ProviderFeed())
    ) as client:
        body = (await client.get("/api/contacts", params={"token": "secret"})).json()
    apps = {a["key"]: a for a in body["apps"]}
    assert apps["gmail"]["connected"] is True


async def test_add_contact_writes_through_the_manager_and_audits() -> None:
    manager = FakeConfigManager()
    audit = FakeAudit()
    async with _client(
        _app(None, config=_contacts_config(), config_manager=manager, audit=audit)
    ) as client:
        response = await client.post(
            "/api/contacts/allowlist",
            params={"token": "secret"},
            json={"platform": "discord", "handle": "@priya"},
        )
    assert response.status_code == 200
    assert manager.set_calls == [
        {
            "op": "add",
            "path": "contacts.allowlist",
            "value": {"platform": "discord", "handle": "@priya"},
            "source": "panel",
        }
    ]
    # the answer is the fresh state, so the page updates in one round-trip
    assert response.json()["mode"] == "enforce"
    # the landing is in the audit chain, the way every config change is
    assert len(audit.entries) == 1
    assert audit.entries[0]["actor"] == "owner"
    assert audit.entries[0]["tool_name"] == "set_config"
    assert audit.entries[0]["outcome"] == "changed from the panel"


async def test_add_contact_refused_by_the_manager_is_a_plain_400() -> None:
    manager = FakeConfigManager(verdict=(False, "telegram/@sam is already on the allowlist."))
    async with _client(_app(None, config=_contacts_config(), config_manager=manager)) as client:
        response = await client.post(
            "/api/contacts/allowlist",
            params={"token": "secret"},
            json={"platform": "telegram", "handle": "@sam"},
        )
    assert response.status_code == 400
    assert response.json()["detail"] == "telegram/@sam is already on the allowlist."


async def test_add_contact_needs_a_handle() -> None:
    async with _client(_app(None, config_manager=FakeConfigManager())) as client:
        response = await client.post(
            "/api/contacts/allowlist", params={"token": "secret"}, json={"handle": "  "}
        )
    assert response.status_code == 400
    assert "needs a handle" in response.json()["detail"]


async def test_remove_contact_names_the_exact_entry() -> None:
    manager = FakeConfigManager()
    async with _client(_app(None, config=_contacts_config(), config_manager=manager)) as client:
        response = await client.request(
            "DELETE",
            "/api/contacts/allowlist",
            params={"token": "secret"},
            json={"platform": "telegram", "handle": "@sam"},
        )
    assert response.status_code == 200
    assert manager.set_calls == [
        {
            "op": "remove",
            "path": "contacts.allowlist",
            "value": {"platform": "telegram", "handle": "@sam"},
            "source": "panel",
        }
    ]


async def test_contacts_mode_round_trips_and_rejects_the_unknown() -> None:
    manager = FakeConfigManager()
    async with _client(_app(None, config=_contacts_config(), config_manager=manager)) as client:
        response = await client.put(
            "/api/contacts/mode", params={"token": "secret"}, json={"mode": "off"}
        )
        assert response.status_code == 200
        bad = await client.put(
            "/api/contacts/mode", params={"token": "secret"}, json={"mode": "strict"}
        )
    assert manager.set_calls == [
        {
            "op": "set",
            "path": "contacts.mode",
            "value": "off",
            "source": "panel",
        }
    ]
    assert bad.status_code == 400
    assert bad.json()["detail"] == "it must be one of: enforce, off"


async def test_contacts_writes_are_503_without_a_manager_wired() -> None:
    async with _client(_app(None, config=_contacts_config())) as client:
        for method, path in (
            ("post", "/api/contacts/allowlist"),
            ("delete", "/api/contacts/allowlist"),
            ("put", "/api/contacts/mode"),
            ("post", "/api/contacts/apps/gmail/allowlist"),
            ("delete", "/api/contacts/apps/gmail/allowlist"),
            ("put", "/api/contacts/apps/gmail/mode"),
        ):
            response = await client.request(
                method, path, params={"token": "secret"}, json={"handle": "@x", "mode": "off"}
            )
            assert response.status_code == 503


async def test_app_contacts_add_and_remove_route_to_the_app_slice() -> None:
    manager = FakeConfigManager()
    audit = FakeAudit()
    async with _client(
        _app(None, config=_contacts_config(), config_manager=manager, audit=audit)
    ) as client:
        add = await client.post(
            "/api/contacts/apps/gmail/allowlist",
            params={"token": "secret"},
            json={"handle": "friend@example.com"},
        )
        await client.request(
            "DELETE",
            "/api/contacts/apps/discord/allowlist",
            params={"token": "secret"},
            json={"handle": "@friend"},
        )
    assert add.status_code == 200
    # the card fixes the platform — only the handle was typed
    assert manager.set_calls == [
        {
            "op": "add",
            "path": "contacts.gmail.allowlist",
            "value": {"platform": "gmail", "handle": "friend@example.com"},
            "source": "panel",
        },
        {
            "op": "remove",
            "path": "contacts.discord.allowlist",
            "value": {"platform": "discord", "handle": "@friend"},
            "source": "panel",
        },
    ]
    assert all(e["outcome"] == "changed from the panel" for e in audit.entries)


async def test_app_contacts_mode_set_off_and_back_to_default() -> None:
    manager = FakeConfigManager()
    async with _client(_app(None, config=_contacts_config(), config_manager=manager)) as client:
        off = await client.put(
            "/api/contacts/apps/gmail/mode", params={"token": "secret"}, json={"mode": "off"}
        )
        back = await client.put(
            "/api/contacts/apps/gmail/mode", params={"token": "secret"}, json={"mode": "default"}
        )
        bad = await client.put(
            "/api/contacts/apps/gmail/mode", params={"token": "secret"}, json={"mode": "strict"}
        )
        stranger = await client.put(
            "/api/contacts/apps/notion/mode", params={"token": "secret"}, json={"mode": "off"}
        )
    assert off.status_code == 200
    assert back.status_code == 200
    # "default" is the reset op — the app follows the section default again
    assert manager.set_calls == [
        {
            "op": "set",
            "path": "contacts.gmail.mode",
            "value": "off",
            "source": "panel",
        },
        {"op": "reset", "path": "contacts.gmail.mode", "value": None, "source": "panel"},
    ]
    assert bad.status_code == 400
    assert bad.json()["detail"] == "it must be one of: enforce, off, default"
    # an app with no allowlist section of its own can't be addressed
    assert stranger.status_code == 404


async def test_app_contacts_routes_are_token_guarded() -> None:
    async with _client(_app(None, config=_contacts_config())) as client:
        assert (
            await client.put("/api/contacts/apps/gmail/mode", json={"mode": "off"})
        ).status_code == 401


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
        ),
        mcp_servers=[
            MCPServerConfig(name="mail", transport=TransportConfig(type="http", url="https://x")),
            # the old app hub's entry is ignored after the cutover
            MCPServerConfig(
                name="composio", transport=TransportConfig(type="http", url="https://hub.test/mcp")
            ),
        ],
        contacts=ContactsConfig(mode="enforce"),
    )
    async with _client(_app(config=config, vision=FakeScreenVision())) as client:
        response = await client.get("/api/settings/apps", params={"token": "secret"})
    data = response.json()
    # no resolver wired → no keys known → has_token is honestly False
    assert {"name": "telegram", "enabled": True, "has_token": False} in data["connectors"]
    assert {"name": "discord", "enabled": False, "has_token": False} in data["connectors"]
    # no tools registry wired → not connected, zero actions
    assert data["mcp_servers"] == [
        {"name": "mail", "transport": "http", "enabled": True, "connected": False, "actions": 0}
    ]
    assert data["screen_vision_available"] is True
    assert data["contacts_mode"] == "enforce"
    # the catalog rides along so the panel never hardcodes the chat-surface
    # list — direct apps come from /api/providers, fetched live
    assert {c["key"] for c in data["catalog"]} == {"telegram", "discord"}
    assert all(set(c) == {"key", "name", "blurb", "kind"} for c in data["catalog"])


async def test_apps_feed_marks_connectors_with_keys_and_live_servers() -> None:
    from aether.config import (
        AppConfig,
        MCPServerConfig,
        MessagingConfig,
        PlatformToggle,
        TransportConfig,
    )

    class _Resolver:
        async def known_names(self):
            return {"TELEGRAM_BOT_TOKEN"}

    class _Tools:
        def mcp_status(self):
            return {"mail": True}

        def server_action_count(self, name):
            return 7 if name == "mail" else 0

    config = AppConfig(
        messaging=MessagingConfig(telegram=PlatformToggle(enabled=True)),
        mcp_servers=[
            MCPServerConfig(name="mail", transport=TransportConfig(type="http", url="https://x")),
        ],
    )
    app = _app(config=config)
    app.state.resolver = _Resolver()
    app.state.tools = _Tools()
    async with _client(app) as client:
        response = await client.get("/api/settings/apps", params={"token": "secret"})
    data = response.json()
    assert {"name": "telegram", "enabled": True, "has_token": True} in data["connectors"]
    assert data["mcp_servers"] == [
        {"name": "mail", "transport": "http", "enabled": True, "connected": True, "actions": 7}
    ]


async def test_apps_feed_reports_screen_vision_unavailable_without_a_provider() -> None:
    async with _client(_app(None)) as client:  # no screen_vision on state
        response = await client.get("/api/settings/apps", params={"token": "secret"})
    assert response.json()["screen_vision_available"] is False


# ---------------------------------------------------------------------------
# /api/traces — decision traces for the web panel
# ---------------------------------------------------------------------------


async def test_traces_feed_is_token_guarded() -> None:
    async with _client(_app(None, traces=FakeTraces())) as client:
        assert (await client.get("/api/traces")).status_code == 401
        assert (await client.get("/api/traces/1")).status_code == 401


async def test_traces_feed_is_503_when_unwired() -> None:
    async with _client(_app(None)) as client:  # no traces on state
        response = await client.get("/api/traces", params={"token": "secret"})
        assert response.status_code == 503
        response = await client.get("/api/traces/1", params={"token": "secret"})
        assert response.status_code == 503


async def test_traces_feed_lists_summaries_newest_first() -> None:
    traces = FakeTraces()
    traces.add(kind="turn", label="telegram · vedant", payload={"calls": []})
    traces.add(kind="carry_out", label="mail__send_message", payload={"result": "sent"})
    async with _client(_app(None, traces=traces)) as client:
        response = await client.get("/api/traces", params={"token": "secret"})
    rows = response.json()["traces"]
    assert [r["id"] for r in rows] == [2, 1]  # newest first
    assert rows[0]["kind"] == "carry_out"
    assert rows[0]["label"] == "mail__send_message"
    assert rows[0]["at"].endswith("+00:00")
    assert "payload" not in rows[0]  # summaries only — the detail endpoint decrypts


async def test_trace_detail_serves_the_full_payload() -> None:
    traces = FakeTraces()
    trace = traces.add(
        kind="turn",
        label="telegram · vedant",
        payload={"trigger": {"messages": []}, "calls": [], "reply": "on it"},
    )
    async with _client(_app(None, traces=traces)) as client:
        response = await client.get(f"/api/traces/{trace.id}", params={"token": "secret"})
    data = response.json()
    assert data["id"] == trace.id
    assert data["kind"] == "turn"
    assert data["payload"]["reply"] == "on it"


async def test_trace_detail_is_404_for_an_unknown_trace() -> None:
    async with _client(_app(None, traces=FakeTraces())) as client:
        response = await client.get("/api/traces/99", params={"token": "secret"})
    assert response.status_code == 404


async def test_trace_detail_backfills_plain_for_legacy_calls() -> None:
    traces = FakeTraces()
    trace = traces.add(
        kind="turn",
        label="telegram · vedant",
        payload={
            "calls": [
                {"name": "mail__list_messages", "params": {}, "decision": "allow"},
                {"name": "mail__send_message", "params": {"to": "a@b.c"}, "plain": "Emailing Sam"},
            ]
        },
    )
    async with _client(_app(None, traces=traces)) as client:
        response = await client.get(f"/api/traces/{trace.id}", params={"token": "secret"})
    calls = response.json()["payload"]["calls"]
    assert calls[0]["plain"] == "Checking mail"  # backfilled from the backend vocabulary
    assert calls[1]["plain"] == "Emailing Sam"  # the recorded sentence wins
    assert "plain" not in traces.traces[0].payload["calls"][0]  # the store is never rewritten


async def test_events_feed_backfills_plain_for_legacy_agent_rows() -> None:
    store = FakeEventStore(
        {
            1: Event(
                id=1,
                source="agent",
                kind="action_done",
                occurred_at=datetime(2026, 9, 25, 10, 1, tzinfo=UTC),
                payload={
                    "tool": "composio__GMAIL_WHO_AM_I",
                    "params": {},
                    "detail": "ok",
                    "approval_id": 1,
                },
                salience_score=5.0,
                memorable=False,
                meta={},
            ),
            2: Event(
                id=2,
                source="agent",
                kind="action_done",
                occurred_at=datetime(2026, 9, 25, 10, 2, tzinfo=UTC),
                payload={
                    "tool": "mail__send_message",
                    "params": {"to": "a@b.c"},
                    "plain": "Emailing Sam",
                    "detail": "sent",
                    "approval_id": 2,
                },
                salience_score=5.0,
                memorable=False,
                meta={},
            ),
        }
    )
    async with _client(_app(None, events=store)) as client:
        response = await client.get("/api/events", params={"token": "secret"})
    by_id = {e["id"]: e for e in response.json()["events"]}
    assert by_id[1]["payload"]["plain"] == "Checked the connected gmail account"
    assert by_id[2]["payload"]["plain"] == "Emailing Sam"  # the recorded sentence wins


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
# /api/proactive/tasks* — the background worker's rows and verdicts
# ---------------------------------------------------------------------------


class FakeProactiveWorker:
    def __init__(self) -> None:
        self.verdicts: list[tuple[int, str]] = []
        self.answer: str | None = "dropped — I'll do less of that."

    async def decide_task(self, task_id: int, verdict: str) -> str | None:
        self.verdicts.append((task_id, verdict))
        return self.answer


_proactive_keys = iter(f"k{i}" for i in range(100))


async def _proactive_row(
    store: FakeProactiveStore, *, status: str = "done", approval_id=None, feedback=None
) -> None:
    task = await store.claim(
        skill="followups",
        key=next(_proactive_keys),
        label="the draft",
        opportunity={},
        day="2026-10-18",
    )
    assert task is not None
    await store.set_plan(task.id, {"approval_id": approval_id} if approval_id else {})
    if status == "done":
        await store.finish(task.id, "the result, in plain words")
    else:
        await store.fail(task.id, "the job died")
    if feedback is not None:
        await store.record_feedback(task.id, feedback)


async def test_proactive_tasks_feed_is_token_guarded() -> None:
    async with _client(_app(None, proactive_store=FakeProactiveStore())) as client:
        assert (await client.get("/api/proactive/tasks")).status_code == 401


async def test_proactive_tasks_feed_is_503_without_a_store_wired() -> None:
    async with _client(_app(None)) as client:
        response = await client.get("/api/proactive/tasks", params={"token": "secret"})
    assert response.status_code == 503


async def test_proactive_tasks_feed_offers_only_live_verdicts() -> None:
    store = FakeProactiveStore()
    await _proactive_row(store)  # plain result — nothing staged
    await _proactive_row(store, approval_id=9)  # staged — all three verdicts
    await _proactive_row(store, approval_id=9, feedback="approved")  # already decided
    await _proactive_row(store, status="failed")  # work that never finished
    async with _client(_app(None, proactive_store=store)) as client:
        response = await client.get("/api/proactive/tasks", params={"token": "secret"})
    rows = response.json()["tasks"]
    # newest first — the failed row was claimed last
    assert [r["options"] for r in rows] == [
        [],
        [],
        ["approve", "revise", "discard"],
        ["approve", "discard"],
    ]
    # the row is plain words the panel can show as-is
    assert rows[3]["label"] == "the draft"
    assert rows[3]["result"] == "the result, in plain words"
    assert rows[3]["skill"] == "followups"  # translated by the panel, never shown raw


async def test_proactive_decide_passes_the_verdict_through() -> None:
    worker = FakeProactiveWorker()
    async with _client(_app(None, proactive_worker=worker)) as client:
        response = await client.post(
            "/api/proactive/tasks/3/decide",
            params={"token": "secret"},
            json={"verdict": "discard"},
        )
    assert response.json() == {"ok": True, "note": "dropped — I'll do less of that."}
    assert worker.verdicts == [(3, "discard")]


async def test_proactive_decide_rejects_an_unknown_verdict_and_a_missing_worker() -> None:
    worker = FakeProactiveWorker()
    async with _client(_app(None, proactive_worker=worker)) as client:
        response = await client.post(
            "/api/proactive/tasks/3/decide",
            params={"token": "secret"},
            json={"verdict": "explode"},
        )
    assert response.status_code == 400
    assert worker.verdicts == []
    async with _client(_app(None)) as client:  # no worker on state
        response = await client.post(
            "/api/proactive/tasks/3/decide",
            params={"token": "secret"},
            json={"verdict": "discard"},
        )
    assert response.status_code == 503


async def test_proactive_decide_is_token_guarded() -> None:
    async with _client(_app(None, proactive_worker=FakeProactiveWorker())) as client:
        response = await client.post("/api/proactive/tasks/3/decide", json={"verdict": "discard"})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# /api/profile* — who the user is, confirmed and pending
# ---------------------------------------------------------------------------


async def test_profile_feed_lists_confirmed_and_pending(tmp_path) -> None:
    ws = _workspace(tmp_path)
    ws.remember("lives in pune", kind="profile", source="user")
    ws.remember("works on databases", kind="profile", source="agent")
    async with _client(_app(None, workspace=ws)) as client:
        assert (await client.get("/api/profile")).status_code == 401
        response = await client.get("/api/profile", params={"token": "secret"})
    body = response.json()
    assert [e["text"] for e in body["confirmed"]] == ["lives in pune"]
    assert [e["text"] for e in body["pending"]] == ["works on databases"]
    assert body["pending"][0]["status"] == "pending"


async def test_profile_confirm_flips_pending_entries(tmp_path) -> None:
    ws = _workspace(tmp_path)
    ws.remember("lives in pune", kind="profile", source="user")
    guessed = ws.remember("works on databases", kind="profile", source="agent")
    async with _client(_app(None, workspace=ws)) as client:
        response = await client.post(
            "/api/profile/confirm", params={"token": "secret"}, json={"ids": [guessed]}
        )
    assert response.json() == {"ok": True, "confirmed": 1}
    assert [e.text for e in ws.profile()] == ["lives in pune", "works on databases"]
    assert ws.pending_profile() == []


async def test_profile_feed_is_503_without_a_workspace_wired() -> None:
    async with _client(_app(None)) as client:
        response = await client.get("/api/profile", params={"token": "secret"})
    assert response.status_code == 503


# ---------------------------------------------------------------------------
# /api/workspace* — the human-editable Markdown identity/memory layer
# ---------------------------------------------------------------------------


def _workspace(tmp_path) -> Workspace:
    ws = Workspace(tmp_path / "ws")
    ws.ensure_scaffold()
    return ws


async def test_workspace_feed_is_token_guarded(tmp_path) -> None:
    async with _client(_app(None, workspace=_workspace(tmp_path))) as client:
        assert (await client.get("/api/workspace")).status_code == 401
        assert (await client.get("/api/workspace/daily")).status_code == 401
        assert (await client.get("/api/workspace/search")).status_code == 401
        assert (await client.put("/api/workspace/soul", json={"text": "x"})).status_code == 401


async def test_workspace_feed_is_503_without_a_workspace_wired() -> None:
    async with _client(_app(None)) as client:  # no workspace on state
        response = await client.get("/api/workspace", params={"token": "secret"})
    assert response.status_code == 503


async def test_workspace_feed_reports_every_file_with_parsed_entries(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    workspace.remember("prefers concise notifications", "preference")
    async with _client(_app(None, workspace=workspace)) as client:
        response = await client.get("/api/workspace", params={"token": "secret"})
    body = response.json()
    assert body["needs_bootstrap"] is True
    assert set(body["files"]) == {"identity", "soul", "agents", "user", "memory"}
    user_entries = body["files"]["user"]["entries"]
    assert any(
        e["text"] == "prefers concise notifications" and e["source"] == "agent"
        for e in user_entries
    )
    assert "Name: Aether" in body["files"]["identity"]["raw"]


async def test_workspace_write_saves_a_human_edit(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    async with _client(_app(None, workspace=workspace)) as client:
        response = await client.put(
            "/api/workspace/soul",
            params={"token": "secret"},
            json={"text": "# SOUL.md\n\n- Be terse.\n"},
        )
    assert response.json() == {"ok": True}
    assert workspace.read("soul") == "# SOUL.md\n\n- Be terse.\n"


async def test_workspace_write_rejects_secrets_and_bad_files(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    async with _client(_app(None, workspace=workspace)) as client:
        secret = await client.put(
            "/api/workspace/memory",
            params={"token": "secret"},
            json={"text": "api_key: sk-abcdefghijklmnopqrstuvwxyz0123456789"},
        )
        assert secret.status_code == 422

        bad_file = await client.put(
            "/api/workspace/bootstrap",  # not in the editable set
            params={"token": "secret"},
            json={"text": "anything"},
        )
        assert bad_file.status_code == 400

        traversal = await client.put(
            "/api/workspace/..%2F..%2Fetc%2Fpasswd",
            params={"token": "secret"},
            json={"text": "anything"},
        )
        assert traversal.status_code in (400, 404)  # never a filesystem write outside the workspace


async def test_workspace_daily_feed_defaults_to_today(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    workspace.remember("deploy went out", "daily")
    async with _client(_app(None, workspace=workspace)) as client:
        response = await client.get("/api/workspace/daily", params={"token": "secret"})
    body = response.json()
    assert body["date"] == workspace.today()
    assert any(e["text"] == "deploy went out" for e in body["entries"])


async def test_workspace_daily_feed_rejects_a_malformed_date(tmp_path) -> None:
    async with _client(_app(None, workspace=_workspace(tmp_path))) as client:
        response = await client.get(
            "/api/workspace/daily", params={"token": "secret", "date": "not-a-date"}
        )
    assert response.status_code == 400


async def test_workspace_search_feed_finds_entries(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    workspace.remember("the user is working on Aether", "fact", category="Projects")
    async with _client(_app(None, workspace=workspace)) as client:
        response = await client.get(
            "/api/workspace/search", params={"token": "secret", "query": "working on Aether"}
        )
    hits = response.json()["hits"]
    assert any("working on Aether" in h["text"] for h in hits)


# ---------------------------------------------------------------------------
# /api/settings/fields + /api/settings/credentials — the editable surface
# ---------------------------------------------------------------------------


class _Resolver:
    """Name-only secret resolver double — never a value."""

    def __init__(self, known: set[str] | None = None) -> None:
        self.known = known or {"TELEGRAM_BOT_TOKEN"}

    async def known_names(self) -> set[str]:
        return self.known


class _SecretStore:
    """SecretStore double: records what landed; the value never leaves."""

    def __init__(self) -> None:
        self.stored: dict[str, str] = {}

    async def set(self, name: str, value: str) -> None:
        self.stored[name] = value

    async def names(self) -> set[str]:
        return set(self.stored)


async def test_settings_fields_are_token_guarded() -> None:
    async with _client(_app(None, config_manager=FakeConfigManager())) as client:
        for method, path in (
            ("get", "/api/settings/fields"),
            ("put", "/api/settings/fields/agent.tick_seconds"),
            ("delete", "/api/settings/fields/agent.tick_seconds"),
            ("put", "/api/settings/credentials/TELEGRAM_BOT_TOKEN"),
        ):
            response = await client.request(method, path, json={"value": "x"})
            assert response.status_code == 401


async def test_settings_fields_are_503_without_a_manager_wired() -> None:
    async with _client(_app(None)) as client:
        for method, path in (
            ("get", "/api/settings/fields"),
            ("put", "/api/settings/fields/agent.tick_seconds"),
            ("delete", "/api/settings/fields/agent.tick_seconds"),
            ("put", "/api/settings/credentials/TELEGRAM_BOT_TOKEN"),
        ):
            response = await client.request(
                method, path, params={"token": "secret"}, json={"value": "x"}
            )
            assert response.status_code == 503


async def test_settings_feed_carries_groups_kinds_and_apply_notes() -> None:
    manager = FakeConfigManager(
        effective_payload={
            "sections": {},
            "overrides": [
                {"path": "salience.threshold", "value": 5},
                {"path": "agent.personality", "value": "warmer"},
            ],
        }
    )
    app = _app(None, config=AppConfig(), config_manager=manager)
    app.state.resolver = _Resolver()
    async with _client(app) as client:
        response = await client.get("/api/settings/fields", params={"token": "secret"})
    data = response.json()
    groups = {g["key"]: g for g in data["groups"]}
    # the model group is the one that restarts, and says so up front
    assert groups["llm"]["restart"] is True
    assert groups["llm"]["note"]
    assert all(not g["restart"] for k, g in groups.items() if k != "llm")
    by_path = {f["path"]: f for g in data["groups"] for f in g["fields"]}
    # kind and choices come from the model's own annotations; the provider
    # field is gone from the page — every model name carries its own
    # service in front now, and the key itself stays for chat's bare ids
    assert "llm.provider" not in by_path
    # kind and choices come from the model's own annotations
    assert by_path["llm.max_tokens"]["kind"] == "number"
    # capabilities are model fields, not per-provider toggles
    assert by_path["llm.image_model"]["kind"] == "model"
    assert "llm.ollama_vision" not in by_path
    assert "llm.nvidia_vision" not in by_path
    assert by_path["agent.quiet_hours"]["kind"] == "text"
    assert by_path["agent.quiet_hours"]["none_ok"] is True
    # the messaging toggles enumerate from the live config
    assert by_path["messaging.telegram.enabled"]["kind"] == "toggle"
    assert by_path["messaging.telegram.enabled"]["value"] is False
    # the plain-words apply notes ride along, chat's own wording
    assert by_path["agent.tick_seconds"]["note"] == "applies from the next tick"
    # changed markers reflect the override rows
    assert by_path["salience.threshold"]["changed"] is True
    assert by_path["agent.tick_seconds"]["changed"] is False
    # credentials are names and set flags only — a value never rides back
    creds = {c["name"]: c for c in data["credentials"]}
    assert set(creds) == {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "NVIDIA_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "DISCORD_BOT_TOKEN",
        "TAVILY_API_KEY",
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "VERCEL_TOKEN",
        "VERCEL_TEAM_ID",
        "VERCEL_PROJECT_ID",
        "GITHUB_ACTIONS_TOKEN",
    }
    assert creds["TELEGRAM_BOT_TOKEN"]["set"] is True
    assert creds["DISCORD_BOT_TOKEN"]["set"] is False
    assert all(set(c) == {"name", "label", "hint", "set"} for c in data["credentials"])


async def test_model_fields_render_as_pickers_with_their_sentinels() -> None:
    app = _app(None, config=AppConfig(), config_manager=FakeConfigManager())
    app.state.resolver = _Resolver()
    async with _client(app) as client:
        response = await client.get("/api/settings/fields", params={"token": "secret"})
    by_path = {f["path"]: f for g in response.json()["groups"] for f in g["fields"]}
    # every model field is a picker, not a text box — the free-text era ends
    for path in (
        "llm.model",
        "llm.image_model",
        "llm.audio_model",
        "llm.video_model",
        "llm.salience_model",
        "llm.embedding_model",
    ):
        assert by_path[path]["kind"] == "model", path
        assert by_path[path]["choices"] == []
    # the main model has no sentinel — something has to think; the senses
    # offer the honest off and the main model; the first pass both; and
    # memory search is a forced choice of a real model, so none at all
    assert by_path["llm.model"]["sentinels"] == []
    assert by_path["llm.image_model"]["sentinels"] == [
        {"value": "off", "label": "None (off)"},
        {"value": "default", "label": "Use default"},
    ]
    assert by_path["llm.audio_model"]["sentinels"] == by_path["llm.video_model"]["sentinels"]
    assert by_path["llm.salience_model"]["sentinels"] == [
        {"value": "default", "label": "Use default"},
        {"value": "off", "label": "None (off)"},
    ]
    assert by_path["llm.embedding_model"]["sentinels"] == []
    # the provider field is gone from the page feed; the key itself
    # stays for chat's bare ids
    assert "llm.provider" not in by_path
    # everything else keeps its annotation-derived kind
    assert by_path["llm.max_tokens"]["kind"] == "number"


class _ModelCatalog:
    """Catalog double: a canned feed that records the refresh flag."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.refreshed: list[bool] = []

    async def feed(self, *, refresh: bool = False) -> dict:
        self.refreshed.append(refresh)
        return self.payload


async def test_the_models_endpoint_lists_configured_providers() -> None:
    catalog = _ModelCatalog(
        {
            "configured": True,
            "providers": [
                {"provider": "anthropic", "models": ["claude-opus-5"], "error": ""},
                {"provider": "gemini", "models": ["gemini-2.5-flash"], "error": ""},
            ],
        }
    )
    app = _app(None)
    app.state.model_catalog = catalog
    async with _client(app) as client:
        denied = await client.get("/api/settings/models")
        assert denied.status_code == 401
        response = await client.get("/api/settings/models", params={"token": "secret"})
        refreshed = await client.get(
            "/api/settings/models", params={"token": "secret", "refresh": "1"}
        )
    data = response.json()
    assert data["configured"] is True
    assert [g["provider"] for g in data["providers"]] == ["anthropic", "gemini"]
    assert refreshed.json() == data
    assert catalog.refreshed == [False, True]  # the manual refresh rides through


async def test_the_models_endpoint_is_503_without_a_catalog_wired() -> None:
    async with _client(_app(None)) as client:
        response = await client.get("/api/settings/models", params={"token": "secret"})
    assert response.status_code == 503


async def test_setting_write_round_trips_and_audits() -> None:
    manager = FakeConfigManager(set_reply="agent.tick_seconds set to 15")
    audit = FakeAudit()
    async with _client(_app(None, config_manager=manager, audit=audit)) as client:
        response = await client.put(
            "/api/settings/fields/agent.tick_seconds",
            params={"token": "secret"},
            json={"value": 15},
        )
    assert response.status_code == 200
    body = response.json()
    # the manager's plain reply rides back as the toast text
    assert body["reply"] == "agent.tick_seconds set to 15"
    # the fresh feed rides back with it — one round-trip
    assert "groups" in body["feed"]
    assert manager.set_calls == [
        {"op": "set", "path": "agent.tick_seconds", "value": 15, "source": "panel"}
    ]
    assert audit.entries[0]["params"] == {"path": "agent.tick_seconds", "op": "set"}
    assert audit.entries[0]["outcome"] == "changed from the panel"


async def test_setting_refusal_is_the_managers_plain_words() -> None:
    manager = FakeConfigManager(verdict=(False, "it needs to be a number"))
    async with _client(_app(None, config_manager=manager)) as client:
        response = await client.put(
            "/api/settings/fields/agent.tick_seconds",
            params={"token": "secret"},
            json={"value": "soon"},
        )
    assert response.status_code == 400
    assert response.json()["detail"] == "it needs to be a number"


async def test_setting_paths_must_come_from_the_catalog() -> None:
    manager = FakeConfigManager()
    async with _client(_app(None, config_manager=manager)) as client:
        stranger = await client.put(
            "/api/settings/fields/contacts.mode",
            params={"token": "secret"},
            json={"value": "off"},
        )
        unknown = await client.put(
            "/api/settings/fields/no.such.path",
            params={"token": "secret"},
            json={"value": 1},
        )
        reset = await client.delete(
            "/api/settings/fields/salience.threshold", params={"token": "secret"}
        )
    # contacts has its own page; a stranger can't mint manager paths here
    assert stranger.status_code == 404
    assert unknown.status_code == 404
    # the reset route asks for op="reset" and no value
    assert reset.status_code == 200
    assert manager.set_calls == [
        {"op": "reset", "path": "salience.threshold", "value": None, "source": "panel"}
    ]


async def test_credential_paste_is_write_only_and_audits_the_name() -> None:
    manager = FakeConfigManager()
    audit = FakeAudit()
    store = _SecretStore()
    app = _app(None, config_manager=manager, audit=audit)
    app.state.secrets = store
    app.state.resolver = _Resolver()
    async with _client(app) as client:
        response = await client.put(
            "/api/settings/credentials/DISCORD_BOT_TOKEN",
            params={"token": "secret"},
            json={"value": "discord-token-1"},
        )
    assert response.status_code == 200
    assert store.stored == {"DISCORD_BOT_TOKEN": "discord-token-1"}
    # the audit row carries the name only — the value is nowhere in it
    assert audit.entries[0]["tool_name"] == "store_app_secret"
    assert audit.entries[0]["params"] == {"name": "DISCORD_BOT_TOKEN"}
    assert audit.entries[0]["outcome"] == "stored encrypted — value never recorded"
    # the response is the fresh feed — the value never rides back out
    text = response.text
    assert "discord-token-1" not in text
    assert {c["name"] for c in response.json()["credentials"]}


async def test_credential_paste_reconnects_an_enabled_surface() -> None:
    manager = FakeConfigManager()
    store = _SecretStore()
    config = AppConfig(messaging=MessagingConfig(telegram=PlatformToggle(enabled=True)))
    app = _app(None, config=config, config_manager=manager)
    app.state.secrets = store
    app.state.resolver = _Resolver()
    async with _client(app) as client:
        response = await client.put(
            "/api/settings/credentials/TELEGRAM_BOT_TOKEN",
            params={"token": "secret"},
            json={"value": "fresh-telegram-token"},
        )
    assert response.status_code == 200
    # the same-value toggle write is what reconciles connectors with the
    # fresh token — it fires only for a surface that is on
    assert manager.set_calls == [
        {
            "op": "set",
            "path": "messaging.telegram.enabled",
            "value": True,
            "source": "panel",
        }
    ]


async def test_credential_paste_for_a_disabled_surface_waits_for_the_toggle() -> None:
    manager = FakeConfigManager()
    store = _SecretStore()
    app = _app(None, config=AppConfig(), config_manager=manager)
    app.state.secrets = store
    app.state.resolver = _Resolver()
    async with _client(app) as client:
        response = await client.put(
            "/api/settings/credentials/DISCORD_BOT_TOKEN",
            params={"token": "secret"},
            json={"value": "discord-token-1"},
        )
    assert response.status_code == 200
    # nothing to reconcile — the surface's own toggle write will do it
    assert manager.set_calls == []


async def test_credential_paste_refuses_the_unknown_and_the_empty() -> None:
    manager = FakeConfigManager()
    store = _SecretStore()
    app = _app(None, config_manager=manager)
    app.state.secrets = store
    async with _client(app) as client:
        stranger = await client.put(
            "/api/settings/credentials/EVIL_NAME",
            params={"token": "secret"},
            json={"value": "x"},
        )
        empty = await client.put(
            "/api/settings/credentials/DISCORD_BOT_TOKEN",
            params={"token": "secret"},
            json={"value": "   "},
        )
    assert stranger.status_code == 404
    assert empty.status_code == 400
    assert store.stored == {}


# -- one-time workbench transfers ------------------------------------------------


class FakeTransfers:
    def __init__(self, bundle: dict | None = None, accepts_results: bool = True) -> None:
        self.bundle = bundle
        self.accepts_results = accepts_results
        self.bundles_taken: list[str] = []
        self.results_posted: list[tuple[str, dict]] = []

    async def take_bundle(self, token: str) -> dict | None:
        self.bundles_taken.append(token)
        return self.bundle

    async def post_result(self, token: str, result: dict) -> bool:
        self.results_posted.append((token, result))
        return self.accepts_results


async def test_a_runner_pulls_its_bundle_without_the_api_token() -> None:
    # the one-time URL is the entire authentication — no token header
    transfers = FakeTransfers(bundle={"script_name": "job.py", "files": {"job.py": "print(1)"}})
    async with _client(_app(None, transfers=transfers)) as client:
        response = await client.get("/api/workbench/bundle/a-one-time-token")
    assert response.status_code == 200
    assert response.json() == {"script_name": "job.py", "files": {"job.py": "print(1)"}}
    assert transfers.bundles_taken == ["a-one-time-token"]


async def test_a_spent_or_unknown_bundle_url_is_simply_not_found() -> None:
    transfers = FakeTransfers(bundle=None)
    async with _client(_app(None, transfers=transfers)) as client:
        response = await client.get("/api/workbench/bundle/already-used")
    assert response.status_code == 404


async def test_a_runner_posts_its_result_once() -> None:
    transfers = FakeTransfers()
    async with _client(_app(None, transfers=transfers)) as client:
        response = await client.post(
            "/api/workbench/result/another-one-time-token",
            json={"exit_code": 0, "stdout": "done", "stderr": ""},
        )
    assert response.status_code == 200
    assert response.json() == {"stored": True}
    assert transfers.results_posted == [
        ("another-one-time-token", {"exit_code": 0, "stdout": "done", "stderr": ""})
    ]


async def test_a_refused_result_post_is_not_found_not_an_error() -> None:
    transfers = FakeTransfers(accepts_results=False)
    async with _client(_app(None, transfers=transfers)) as client:
        response = await client.post(
            "/api/workbench/result/spent",
            json={"exit_code": 1, "stdout": "", "stderr": "boom"},
        )
    assert response.status_code == 404


async def test_the_transfer_kinds_only_answer_their_own_verbs() -> None:
    # GET answers only bundles, POST only results — anything else is not found
    transfers = FakeTransfers(bundle={"script_name": "job.py", "files": {}})
    async with _client(_app(None, transfers=transfers)) as client:
        get_result = await client.get("/api/workbench/result/some-token")
        post_bundle = await client.post(
            "/api/workbench/bundle/some-token", json={"exit_code": 0, "stdout": "", "stderr": ""}
        )
    assert get_result.status_code == 404
    assert post_bundle.status_code == 404


async def test_transfers_not_wired_answers_503() -> None:
    async with _client(_app(None)) as client:
        get = await client.get("/api/workbench/bundle/some-token")
        post = await client.post(
            "/api/workbench/result/some-token", json={"exit_code": 0, "stdout": "", "stderr": ""}
        )
    assert get.status_code == 503
    assert post.status_code == 503


# ---------------------------------------------------------------------------
# /api/media — stored files served to the panel, uploads from it


class FakeMediaRecord:
    """The MediaRecord shape the real store hands back: sniffed type,
    stored name, decrypted bytes."""

    def __init__(self, media_id: int, media_type: str, filename: str, data: bytes) -> None:
        self.media_id = media_id
        self.media_type = media_type
        self.filename = filename
        self.size = len(data)
        self.data = data


class FakeMediaStore:
    def __init__(self, rows: dict | None = None) -> None:
        self.rows = rows or {}

    async def get(self, media_id: int):
        return self.rows.get(media_id)


def _png() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"real-png-bytes"


async def test_media_serves_an_inline_image_with_hardening_headers() -> None:
    store = FakeMediaStore({1: FakeMediaRecord(1, "image/png", "plan.png", _png())})
    async with _client(_app(media_store=store)) as client:
        response = await client.get("/api/media/1", params={"token": "secret"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"
    assert "content-disposition" not in response.headers
    assert response.content == _png()


async def test_media_forces_a_download_for_anything_not_renderable() -> None:
    """The declared MIME at ingest was metadata only — the sniffed type
    decides, and a sniffed-but-unrenderable file is a download, never an
    inline document. A stored filename is user input: quotes and newlines
    must never reach the disposition header."""
    hostile = FakeMediaRecord(2, "application/zip", 'bad"name\n.png', b"PK\x03\x04archived")
    store = FakeMediaStore({2: hostile})
    async with _client(_app(media_store=store)) as client:
        response = await client.get("/api/media/2", params={"token": "secret"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/octet-stream")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment; filename=")
    # the two format quotes are the header's own — the name inside them
    # must carry none of the quotes or newlines the stored one had
    inner = disposition[len('attachment; filename="') : -1]
    assert inner == "badname.png"


async def test_media_serves_inline_video_and_audio_only_when_sniffed() -> None:
    store = FakeMediaStore(
        {
            3: FakeMediaRecord(3, "video/mp4", "tour.mp4", b"\x00\x00\x00\x18ftypisom"),
            4: FakeMediaRecord(4, "audio/ogg", "note.oga", b"OggS"),
        }
    )
    async with _client(_app(media_store=store)) as client:
        video = await client.get("/api/media/3", params={"token": "secret"})
        audio = await client.get("/api/media/4", params={"token": "secret"})
    assert video.headers["content-type"].startswith("video/mp4")
    assert "content-disposition" not in video.headers
    assert audio.headers["content-type"].startswith("audio/ogg")
    assert "content-disposition" not in audio.headers


async def test_media_gone_blob_is_the_plain_404_note() -> None:
    store = FakeMediaStore({})  # evicted — nothing stored
    async with _client(_app(media_store=store)) as client:
        response = await client.get("/api/media/99", params={"token": "secret"})
    assert response.status_code == 404
    assert response.json()["detail"] == "that file is no longer stored"


async def test_media_requires_the_token_and_a_store() -> None:
    async with _client(_app()) as client:  # no media_store on state at all
        unauthorized = await client.get("/api/media/1")
        assert unauthorized.status_code == 401  # the token gates first
        missing = await client.get("/api/media/1", params={"token": "secret"})
    assert missing.status_code == 404  # nothing wired reads as gone, never 500


async def test_media_upload_rides_an_inbound_to_the_loop() -> None:
    agent = FakeAgent()
    async with _client(_app(agent=agent)) as client:
        response = await client.post(
            "/api/media",
            params={"token": "secret"},
            files={"file": ("plan.png", _png(), "image/png")},
            data={"note": "the launch sketch"},
        )
    assert response.status_code == 202
    assert response.json() == {"accepted": True, "kind": "image", "filename": "plan.png"}
    # the bytes ride an inbound like any chat attachment — the normal
    # sharing path stores and describes them
    (message,) = agent.submitted
    assert message.surface == "web"
    assert message.text == "the launch sketch"
    (attachment,) = message.attachments
    assert attachment.kind == "image"
    assert attachment.data == _png()


async def test_media_upload_guards_the_same_way_screen_capture_does() -> None:
    async with _client(_app()) as client:  # no agent wired
        refused = await client.post(
            "/api/media",
            params={"token": "secret"},
            files={"file": ("a.png", b"x", "image/png")},
        )
        assert refused.status_code == 503
    agent = FakeAgent()
    async with _client(_app(agent=agent)) as client:
        bad_token = await client.post(
            "/api/media",
            params={"token": "wrong"},
            files={"file": ("a.png", b"x", "image/png")},
        )
        assert bad_token.status_code == 401
        empty = await client.post(
            "/api/media",
            params={"token": "secret"},
            files={"file": ("a.png", b"", "image/png")},
        )
        assert empty.status_code == 400
        too_big = await client.post(
            "/api/media",
            params={"token": "secret"},
            files={"file": ("a.png", b"x" * (20 * 1024 * 1024 + 1), "image/png")},
        )
        assert too_big.status_code == 413
    assert agent.submitted == []
