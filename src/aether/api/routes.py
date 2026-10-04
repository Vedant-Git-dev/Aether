"""REST endpoints. `settings` and component instances are read from
`request.app.state`, which the lifespan in main.py populates before the
server accepts traffic. Everything except /healthz and /oauth/callback is
token-guarded — the callback is its own proof (its state is HMAC-signed).
"""

from __future__ import annotations

import hmac
import html
import logging

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..apps_catalog import CATALOG, recipe_for
from ..oauth import OAuthError, OAuthFlow, exchange_code

log = logging.getLogger("aether.api")

router = APIRouter()

# a full-screen PNG is a few MB; anything past this is not a screenshot
MAX_CAPTURE_BYTES = 20 * 1024 * 1024


def _authorized(request: Request, token: str | None) -> bool:
    candidate = token or request.headers.get("x-aether-token", "")
    return bool(candidate) and hmac.compare_digest(candidate, request.app.state.settings.api_token)


class DecisionBody(BaseModel):
    decision: str  # "approve" | "deny"


class PersonalityBody(BaseModel):
    text: str


def _oauth_page(title: str, body: str, status_code: int) -> HTMLResponse:
    """The tiny standalone page the browser round-trip ends on — a few
    plain words, never the panel (which is not ours to shape)."""
    return HTMLResponse(
        f'<!doctype html><html><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title></head>"
        f'<body style="font-family: system-ui, sans-serif; max-width: 34rem; '
        f'margin: 4rem auto; padding: 0 1rem; line-height: 1.6; color: #1a1a1a">'
        f"<h2>{html.escape(title)}</h2><p>{body}</p></body></html>",
        status_code=status_code,
    )


@router.get("/oauth/callback")
async def oauth_callback(request: Request, code: str = "", state: str = "", error: str = "") -> HTMLResponse:
    """Where a Google sign-in lands. Public on purpose: the state riding
    the consent link is HMAC-signed with the encryption key and expires in
    ten minutes, so the link itself is the proof — AETHER_TOKEN never
    appears in a URL. Verifies the state, exchanges the code for tokens,
    stores them encrypted (the one narrow receive-channel; no secret is
    ever typed in chat), and hands the connection to the loop, which
    answers in chat. The page says so in three words and the tab closes."""
    if error or not code:
        # refused on Google's own screen, or an incomplete round-trip —
        # nothing was received either way
        return _oauth_page(
            "Sign-in wasn't completed",
            "Nothing was received. Close this tab — if you still want to "
            "connect the app, say <code>/apps</code> in chat and start again.",
            200,
        )
    settings = request.app.state.settings
    try:
        flow = OAuthFlow(settings.encryption_key)
    except OAuthError as exc:
        log.error("oauth callback refused: %s", exc)
        return _oauth_page("Sign-in can't finish", html.escape(str(exc)), 400)
    verified = flow.verify_state(state) if state else None
    if verified is None:
        return _oauth_page(
            "Sign-in link expired",
            "This link works for ten minutes and only where it was issued. "
            "Close this tab and ask me for a fresh one in chat — "
            "<code>/apps</code>.",
            400,
        )
    provider, app, redirect = verified
    # acts read the resolver, never boot-time settings — but a deployment
    # that never wired one still finishes its round-trip
    resolver = getattr(request.app.state, "resolver", None)
    client_id = client_secret = ""
    if resolver is not None:
        client_id = (await resolver.resolve("GOOGLE_CLIENT_ID")) or ""
        client_secret = (await resolver.resolve("GOOGLE_CLIENT_SECRET")) or ""
    client_id = client_id or settings.google_client_id
    client_secret = client_secret or settings.google_client_secret
    try:
        tokens = await exchange_code(
            provider,
            code=code,
            client_id=client_id,
            client_secret=client_secret,
            redirect=redirect,  # from the signed state — the exact URI registered
        )
    except OAuthError as exc:
        log.warning("oauth exchange failed: %s", exc)
        return _oauth_page(
            "Sign-in failed",
            f"{html.escape(str(exc))} — nothing was stored. "
            "Close this tab and try again from chat.",
            400,
        )
    await request.app.state.oauth_store.save(provider, tokens)
    agent = getattr(request.app.state, "agent", None)
    if agent is not None:
        await agent.finish_oauth(provider, app)
    return _oauth_page(
        "✅ authorized",
        "That's everything — close this tab. The app connects in chat, "
        "with the same one-tap approval as always.",
        200,
    )


@router.post("/api/screen-capture", status_code=202)
async def screen_capture(
    request: Request,
    token: str | None = None,
    file: UploadFile = File(...),
    note: str = Form(""),
) -> dict:
    """The companion's upload point. Perception-only: the screenshot becomes
    a described, encrypted memory event — no action tools come from it."""
    if not _authorized(request, token):
        raise HTTPException(status_code=401, detail="bad or missing token")
    screen_vision = getattr(request.app.state, "screen_vision", None)
    if screen_vision is None:
        raise HTTPException(
            status_code=503,
            detail="screen vision is not configured (no vision-capable provider)",
        )
    png = await file.read(MAX_CAPTURE_BYTES + 1)
    if len(png) > MAX_CAPTURE_BYTES:
        raise HTTPException(status_code=413, detail="screenshot too large")
    if not png:
        raise HTTPException(status_code=400, detail="empty upload")
    result = await screen_vision.record(png, note)
    if result is None:  # defensive — record() only returns None pre-check
        raise HTTPException(status_code=503, detail="no vision provider")
    # fresh eyes for the agent: wake the loop so the capture is observed now
    agent = getattr(request.app.state, "agent", None)
    if agent is not None:
        agent.notify()
    return {"stored": result.stored, "reason": result.reason, "event_id": result.event_id}


@router.get("/api/screen-request")
async def take_screen_request(request: Request, token: str | None = None) -> dict:
    """The companion's poll: is the agent asking for a screenshot? Consumes
    the request (one capture per ask)."""
    if not _authorized(request, token):
        raise HTTPException(status_code=401, detail="bad or missing token")
    box = getattr(request.app.state, "capture_box", None)
    if box is None:
        raise HTTPException(status_code=503, detail="capture requests not wired")
    note = box.take()
    if note is None:
        return Response(status_code=204)  # nothing asked for — poll again later
    return {"note": note}


@router.get("/api/approvals")
async def list_approvals(request: Request, token: str | None = None) -> dict:
    """Pending approvals for the web panel — the same rows the chat
    surfaces show buttons for."""
    if not _authorized(request, token):
        raise HTTPException(status_code=401, detail="bad or missing token")
    approvals = request.app.state.approvals
    pending = await approvals.list_pending()
    return {
        "pending": [
            {
                "id": a.id,
                "tool_name": a.tool_name,
                "params": a.params,
                # why the gate parked it — the card's plain-language reason
                "rules_matched": a.rules_matched,
                "note": a.note,
                "created_at": a.created_at.isoformat(),
                "expires_at": a.expires_at.isoformat(),
            }
            for a in pending
        ]
    }


@router.post("/api/approvals/{approval_id}/decide")
async def decide_approval(
    approval_id: int, body: DecisionBody, request: Request, token: str | None = None
) -> dict:
    """The web panel's approve/deny buttons — the same path a chat-surface
    tap takes: decide, then run a freshly approved call."""
    if not _authorized(request, token):
        raise HTTPException(status_code=401, detail="bad or missing token")
    agent = getattr(request.app.state, "agent", None)
    if agent is None:
        raise HTTPException(status_code=503, detail="agent loop is not running")
    try:
        approval = await agent.decide(approval_id, body.decision)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if approval is None:
        return {"ok": False, "reason": "already decided or expired"}
    return {"ok": True, "status": approval.status}


@router.get("/api/events")
async def events_feed(request: Request, limit: int = 50, token: str | None = None) -> dict:
    """The newest events, decrypted for the owner's eyes only."""
    if not _authorized(request, token):
        raise HTTPException(status_code=401, detail="bad or missing token")
    events = await request.app.state.events.recent(limit=min(limit, 200))
    return {
        "events": [
            {
                "id": e.id,
                "source": e.source,
                "kind": e.kind,
                "at": e.occurred_at.isoformat(),
                "salience": e.salience_score,
                "memorable": e.memorable,
                "payload": e.payload,
            }
            for e in events  # recent() is newest first — the feed's order
        ]
    }


_BUILTIN_RULES = [
    {
        "id": "builtin:config-tune",
        "decision": "allow",
        "description": "Personal tuning from chat: set_config on agent.*, salience.*, or "
        "llm.* — applies right away (llm.* also restarts the agent to load it).",
    },
    {
        "id": "builtin:config-security",
        "decision": "require_approval",
        "description": "set_config on the security sections (contacts, authz, messaging, "
        "mcp_servers) or on an unrecognized path — held for the user's one tap.",
    },
    {
        "id": "builtin:internal",
        "decision": "allow",
        "description": "Aether's own memory, scheduling, capture-request, and web-chat "
        "tools — they only ever touch Aether's own state.",
    },
    {
        "id": "builtin:risky",
        "decision": "require_approval",
        "description": "Verbs that reach an external system or are hard to undo: send, "
        "reply, forward, post, publish, delete, remove, cancel, pay, transfer, "
        "invite, book, create.",
    },
    {
        "id": "builtin:read-only",
        "decision": "allow",
        "description": "Observation verbs: list, search, get, read, fetch, find, query.",
    },
    {
        "id": "default:fail-safe",
        "decision": "require_approval",
        "description": "Anything that matches none of the above — an unknown tool can "
        "never run silently.",
    },
]


@router.get("/api/policy")
async def policy_feed(request: Request, token: str | None = None) -> dict:
    """Read-only view of the authorization policy: the user's own rules from
    config.yaml (evaluated first, in order) followed by the built-in ladder
    every call falls through to. No edit path — config.yaml is the source
    of truth."""
    if not _authorized(request, token):
        raise HTTPException(status_code=401, detail="bad or missing token")
    authz = request.app.state.config.authz
    return {
        "rules": [
            {
                "tool_pattern": r.tool_pattern,
                "param_pattern": r.param_pattern,
                "decision": r.decision,
                "note": r.note,
            }
            for r in authz.rules
        ],
        "builtin_rules": _BUILTIN_RULES,
        "approval_ttl_hours": authz.approval_ttl_hours,
    }


@router.get("/api/config")
async def config_feed(request: Request, token: str | None = None) -> dict:
    """Read-only: the live configuration (config.yaml merged with chat-made
    overrides) plus the override rows themselves. No edit path — changes
    come from chat (/config) through the same approval gate as every other
    tool call, never from the panel."""
    if not _authorized(request, token):
        raise HTTPException(status_code=401, detail="bad or missing token")
    manager = getattr(request.app.state, "config_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="config management is not wired")
    return await manager.effective()


@router.get("/api/settings/personality")
async def get_personality(request: Request, token: str | None = None) -> dict:
    """The owner's custom behavior instructions — appended to the real
    system prompt on every turn, not cosmetic."""
    if not _authorized(request, token):
        raise HTTPException(status_code=401, detail="bad or missing token")
    agent_settings = getattr(request.app.state, "agent_settings", None)
    if agent_settings is None:
        raise HTTPException(status_code=503, detail="agent settings store is not wired")
    return {"text": await agent_settings.get_personality()}


@router.put("/api/settings/personality")
async def set_personality(
    body: PersonalityBody, request: Request, token: str | None = None
) -> dict:
    if not _authorized(request, token):
        raise HTTPException(status_code=401, detail="bad or missing token")
    agent_settings = getattr(request.app.state, "agent_settings", None)
    if agent_settings is None:
        raise HTTPException(status_code=503, detail="agent settings store is not wired")
    saved = await agent_settings.set_personality(body.text)
    return {"ok": True, "text": saved}


@router.get("/api/settings/apps")
async def apps_feed(request: Request, token: str | None = None) -> dict:
    """Read-only view of what's actually connected: the native messaging
    toggles (and whether each one's keys are in place), the MCP servers
    with their live connection state, any OAuth sign-ins (provider and
    scopes only — never tokens), screen-vision availability, and the
    contact allowlist mode. The catalog of connectable apps rides along so
    the panel can show what chat's /apps walk can add. No edit path —
    connecting happens in chat, where keys are pasted and consumed."""
    if not _authorized(request, token):
        raise HTTPException(status_code=401, detail="bad or missing token")
    config = request.app.state.config

    # which key names exist right now — names only, never values
    resolver = getattr(request.app.state, "resolver", None)
    known = await resolver.known_names() if resolver is not None else set()

    def _has_token(platform: str) -> bool:
        recipe = recipe_for(platform)
        if recipe is None or not recipe.env_names:
            return False
        return all(name in known for name in recipe.env_names)

    # live MCP state from the tool registry — a server that's up counts
    # its actions; one that hasn't answered yet reads "still connecting"
    tools = getattr(request.app.state, "tools", None)
    status = tools.mcp_status() if tools is not None else {}

    def _actions(server_name: str) -> int:
        return tools.server_action_count(server_name) if tools is not None else 0

    # OAuth sign-ins: provider + scopes only, so the panel can say what a
    # sign-in covers without ever seeing a token
    oauth_store = getattr(request.app.state, "oauth_store", None)
    stored_tokens = await oauth_store.all() if oauth_store is not None else {}

    return {
        "connectors": [
            {"name": name, "enabled": toggle.enabled, "has_token": _has_token(name)}
            for name, toggle in (
                ("telegram", config.messaging.telegram),
                ("discord", config.messaging.discord),
                ("slack", config.messaging.slack),
            )
        ],
        "mcp_servers": [
            {
                "name": s.name,
                "transport": s.transport.type,
                "enabled": s.enabled,
                "connected": bool(status.get(s.name)),
                "actions": _actions(s.name),
            }
            for s in config.mcp_servers
        ],
        "oauth": [
            {"provider": provider, "scopes": tokens.scopes}
            for provider, tokens in sorted(stored_tokens.items())
        ],
        "screen_vision_available": getattr(request.app.state, "screen_vision", None) is not None,
        "contacts_mode": config.contacts.mode,
        "catalog": [
            {
                "key": r.key,
                "name": r.name,
                "blurb": r.blurb,
                "kind": r.kind,
                "oauth": bool(r.oauth),
                # the mcp_servers entry this recipe writes — how the panel
                # tells "already connected" from "available to add"
                "server_name": str(r.server.get("name", "")),
            }
            for r in CATALOG
        ],
    }


@router.get("/api/audit")
async def audit_feed(request: Request, limit: int = 100, token: str | None = None) -> dict:
    """Recent audit entries plus the live chain verification — the panel
    shows both, so tampering is visible in the same view as the history."""
    if not _authorized(request, token):
        raise HTTPException(status_code=401, detail="bad or missing token")
    audit = request.app.state.audit
    rows = await audit.recent(limit=min(limit, 500))
    chain = await audit.verify_chain()
    return {
        "entries": [
            {
                "seq": r["seq"],
                "actor": r["actor"],
                "tool": r["tool_name"],
                "decision": r["decision"],
                "rules_matched": r["rules_matched"],
                "outcome": r["outcome"],
                "hash": r["entry_hash"],
                "at": r["created_at"].isoformat(),
            }
            for r in rows
        ],
        "chain": {
            "ok": chain.ok,
            "entries": chain.entries,
            "first_bad_seq": chain.first_bad_seq,
            "problem": chain.problem,
            # rows are newest-first, so the first row's hash is the current chain head
            "head_hash": rows[0]["entry_hash"] if rows else None,
        },
    }


@router.get("/api/traces")
async def traces_feed(request: Request, limit: int = 30, token: str | None = None) -> dict:
    """Recent decision traces (the "why" to the audit's "what") for the
    web panel — summaries; the full payload comes per trace."""
    if not _authorized(request, token):
        raise HTTPException(status_code=401, detail="bad or missing token")
    traces = getattr(request.app.state, "traces", None)
    if traces is None:
        raise HTTPException(status_code=503, detail="decision traces not wired")
    rows = await traces.recent(limit=min(limit, 200))
    return {
        "traces": [
            {
                "id": t.id,
                "kind": t.kind,
                "label": t.label,
                "at": t.created_at.isoformat(),
            }
            for t in rows  # recent() is newest first — the feed's order
        ]
    }


@router.get("/api/traces/{trace_id}")
async def trace_detail(
    trace_id: int, request: Request, token: str | None = None
) -> dict:
    """One decision trace, decrypted for the owner's eyes: what triggered
    the act, the gate's ruling on every call, and what came back."""
    if not _authorized(request, token):
        raise HTTPException(status_code=401, detail="bad or missing token")
    traces = getattr(request.app.state, "traces", None)
    if traces is None:
        raise HTTPException(status_code=503, detail="decision traces not wired")
    trace = await traces.get(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail=f"no trace {trace_id}")
    return {
        "id": trace.id,
        "kind": trace.kind,
        "label": trace.label,
        "at": trace.created_at.isoformat(),
        "payload": trace.payload,
    }


@router.get("/api/memory")
async def memory_feed(request: Request, limit: int = 50, token: str | None = None) -> dict:
    """Everyone Aether has resolved a cross-platform identity for, with
    their linked handles and most recent relationship note."""
    if not _authorized(request, token):
        raise HTTPException(status_code=401, detail="bad or missing token")
    entities = getattr(request.app.state, "entities", None)
    if entities is None:
        raise HTTPException(status_code=503, detail="entity store is not wired")
    people = await entities.list_people(limit=min(limit, 200))
    out = []
    for person in people:
        notes = await entities.notes_for(person.id, limit=1)
        out.append(
            {
                "id": person.id,
                "display_name": person.display_name,
                "confidence": person.confidence,
                "handles": [{"platform": p, "handle": h} for p, h in person.handles],
                "latest_note": (
                    {"at": notes[0].created_at.isoformat(), "payload": notes[0].payload}
                    if notes
                    else None
                ),
            }
        )
    return {"people": out}


@router.get("/api/tasks")
async def tasks_feed(request: Request, limit: int = 50, token: str | None = None) -> dict:
    """Scheduled actions — persisted, survive restarts, run through the
    same authorization gate as everything else when they fire."""
    if not _authorized(request, token):
        raise HTTPException(status_code=401, detail="bad or missing token")
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="scheduler is not wired")
    actions = await scheduler.list(limit=min(limit, 200))
    return {
        "tasks": [
            {
                "id": a.id,
                "label": a.label,
                "run_at": a.run_at.isoformat(),
                "status": a.status,
                "payload": a.payload,
                "created_at": a.created_at.isoformat(),
            }
            for a in actions
        ]
    }
