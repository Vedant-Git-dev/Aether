"""REST endpoints. `settings` and component instances are read from
`request.app.state`, which the lifespan in main.py populates before the
server accepts traffic. Everything except /healthz is token-guarded.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel

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
    toggles and MCP servers from config.yaml, whether screen-vision is
    available this boot, and the contact allowlist mode. No edit path —
    config.yaml and provider keys are the source of truth."""
    if not _authorized(request, token):
        raise HTTPException(status_code=401, detail="bad or missing token")
    config = request.app.state.config
    return {
        "connectors": [
            {"name": name, "enabled": toggle.enabled}
            for name, toggle in (
                ("telegram", config.messaging.telegram),
                ("discord", config.messaging.discord),
                ("slack", config.messaging.slack),
            )
        ],
        "mcp_servers": [
            {"name": s.name, "transport": s.transport.type, "enabled": s.enabled}
            for s in config.mcp_servers
        ],
        "screen_vision_available": getattr(request.app.state, "screen_vision", None) is not None,
        "contacts_mode": config.contacts.mode,
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
