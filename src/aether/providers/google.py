"""Google: Gmail and Calendar over one shared OAuth connection."""

from __future__ import annotations

import asyncio
import base64
import re
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote

from .types import (
    AppDefinition,
    PollDefinition,
    ProviderDefinition,
    ProviderHTTPError,
    ToolDefinition,
)

GMAIL_READ = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
CALENDAR_READ = "https://www.googleapis.com/auth/calendar.readonly"
CALENDAR_EVENTS = "https://www.googleapis.com/auth/calendar.events"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"

_GOOGLE_SETUP_URL = "https://console.cloud.google.com/apis/credentials"
_GOOGLE_SETUP_STEPS = (
    "Open Google Cloud Console and create or select a project.",
    "Enable the Gmail API and Google Calendar API under APIs and Services, then Library.",
    "On the OAuth consent screen, choose External and add the exact Google account "
    "you will sign in with as a test user.",
    "Create credentials, choose OAuth client ID, then Web application.",
    "Add the redirect URI shown here as an authorized redirect URI.",
    "Paste the client ID and client secret into Aether when asked.",
)


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"successful": True, "data": data}


def _err(message: str) -> dict[str, Any]:
    return {"successful": False, "error": message}


def _header(payload: dict[str, Any], name: str) -> str:
    wanted = name.lower()
    for header in payload.get("headers", []) or []:
        if str(header.get("name", "")).lower() == wanted:
            return str(header.get("value", ""))
    return ""


def _decode_body(data: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (ValueError, TypeError):
        return ""
    return raw.decode("utf-8", errors="replace")


class _TextFromHtml(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.parts)).strip()


def _plain_text(payload: dict[str, Any]) -> str:
    mime = str(payload.get("mimeType", ""))
    body = payload.get("body") or {}
    data = body.get("data")
    if data:
        text = _decode_body(str(data))
        if mime == "text/html":
            parser = _TextFromHtml()
            parser.feed(text)
            return parser.text()
        return text
    parts = payload.get("parts") or []
    plain = ""
    html = ""
    for part in parts:
        text = _plain_text(part)
        if not text:
            continue
        if str(part.get("mimeType", "")) == "text/html":
            html = html or text
        else:
            plain = plain or text
    return plain or html


def _summary(message: dict[str, Any]) -> dict[str, Any]:
    payload = message.get("payload") or {}
    return {
        "id": str(message.get("id", "")),
        "thread_id": str(message.get("threadId", "")),
        "sender": _header(payload, "From"),
        "to": _header(payload, "To"),
        "cc": _header(payload, "Cc"),
        "subject": _header(payload, "Subject"),
        "date": _header(payload, "Date"),
        "snippet": str(message.get("snippet", "")),
    }


async def _get_summary(ctx: Any, message_id: str) -> dict[str, Any]:
    data = await ctx.json(
        "GET",
        f"/gmail/v1/users/me/messages/{message_id}",
        params={"format": "metadata"},
    )
    return _summary(data)


async def _summaries(ctx: Any, ids: list[str]) -> list[dict[str, Any]]:
    """Fetch metadata for a batch, bounded and best effort: one message
    that vanished between the list and the read never sinks the batch."""
    semaphore = asyncio.Semaphore(5)

    async def one(message_id: str) -> dict[str, Any] | None:
        if not message_id:
            return None
        async with semaphore:
            try:
                return await _get_summary(ctx, message_id)
            except ProviderHTTPError:
                return None

    return [item for item in await asyncio.gather(*(one(i) for i in ids)) if item]


async def list_messages(ctx: Any, params: dict[str, Any]) -> Any:
    query = str(params.get("query") or "").strip()
    page_token = str(params.get("page_token") or "").strip()
    try:
        max_results = int(params.get("max_results") or 10)
    except (TypeError, ValueError):
        max_results = 10
    max_results = max(1, min(max_results, 50))
    request_params: dict[str, Any] = {"maxResults": max_results}
    if query:
        request_params["q"] = query
    if page_token:
        request_params["pageToken"] = page_token
    data = await ctx.json("GET", "/gmail/v1/users/me/messages", params=request_params)
    ids = [str(item.get("id", "")) for item in data.get("messages", []) or []]
    summaries = await _summaries(ctx, ids)
    return _ok(
        {
            "messages": summaries,
            "next_page_token": str(data.get("nextPageToken", "")),
        }
    )


async def get_message(ctx: Any, params: dict[str, Any]) -> Any:
    message_id = str(params.get("message_id") or "").strip()
    if not message_id:
        return {"successful": False, "error": "a message id is required"}
    data = await ctx.json(
        "GET",
        f"/gmail/v1/users/me/messages/{message_id}",
        params={"format": "full"},
    )
    summary = _summary(data)
    summary["body"] = _plain_text(data.get("payload") or {})
    summary["label_ids"] = list(data.get("labelIds", []) or [])
    # the same envelope list_messages uses, so the allowlist filter can
    # judge a single read too (and there is no unfiltered side channel)
    return _ok({"messages": [summary]})


def _addresses(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _raw_message(params: dict[str, Any]) -> str:
    message = EmailMessage()
    message["To"] = _addresses(params.get("to"))
    cc = _addresses(params.get("cc"))
    bcc = _addresses(params.get("bcc"))
    if cc:
        message["Cc"] = cc
    if bcc:
        message["Bcc"] = bcc
    message["Subject"] = str(params.get("subject") or "")
    message.set_content(str(params.get("body") or ""))
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


async def send_email(ctx: Any, params: dict[str, Any]) -> Any:
    payload: dict[str, Any] = {"raw": _raw_message(params)}
    thread_id = str(params.get("thread_id") or "").strip()
    if thread_id:
        payload["threadId"] = thread_id
    data = await ctx.json("POST", "/gmail/v1/users/me/messages/send", json=payload)
    return _ok(
        {
            "id": str(data.get("id", "")),
            "thread_id": str(data.get("threadId", "")),
            "label_ids": list(data.get("labelIds", []) or []),
        }
    )


async def create_draft(ctx: Any, params: dict[str, Any]) -> Any:
    message: dict[str, Any] = {"raw": _raw_message(params)}
    thread_id = str(params.get("thread_id") or "").strip()
    if thread_id:
        message["threadId"] = thread_id
    data = await ctx.json("POST", "/gmail/v1/users/me/drafts", json={"message": message})
    returned = data.get("message") or {}
    return _ok(
        {
            "id": str(data.get("id", "")),
            "message": {
                "id": str(returned.get("id", "")),
                "thread_id": str(returned.get("threadId", "")),
            },
        }
    )


async def send_draft(ctx: Any, params: dict[str, Any]) -> Any:
    draft_id = str(params.get("draft_id") or "").strip()
    if not draft_id:
        return {"successful": False, "error": "a draft id is required"}
    data = await ctx.json("POST", "/gmail/v1/users/me/drafts/send", json={"id": draft_id})
    return _ok(
        {
            "id": str(data.get("id", "")),
            "thread_id": str(data.get("threadId", "")),
        }
    )


async def get_profile(ctx: Any, _params: dict[str, Any]) -> Any:
    data = await ctx.json("GET", "/gmail/v1/users/me/profile")
    return _ok(
        {
            "email_address": str(data.get("emailAddress", "")),
            "messages_total": int(data.get("messagesTotal") or 0),
            "threads_total": int(data.get("threadsTotal") or 0),
            "history_id": str(data.get("historyId", "")),
        }
    )


async def poll_new_mail(ctx: Any) -> Any:
    profile = await ctx.json("GET", "/gmail/v1/users/me/profile")
    history_id = str(profile.get("historyId", ""))
    last = await ctx.get_sync("history_id")
    if not last:
        if history_id:
            await ctx.set_sync("history_id", history_id)
        return None
    try:
        data = await ctx.json(
            "GET",
            "/gmail/v1/users/me/history",
            params={
                "startHistoryId": last,
                "historyTypes": "messageAdded",
                "maxResults": 25,
            },
        )
    except ProviderHTTPError as exc:
        if exc.status != 404:
            raise
        if history_id:
            await ctx.set_sync("history_id", history_id)
        recent = await ctx.json(
            "GET",
            "/gmail/v1/users/me/messages",
            params={"maxResults": 5, "q": "newer_than:1d"},
        )
        ids = [str(item.get("id", "")) for item in recent.get("messages", []) or []]
        summaries = await _summaries(ctx, ids)
        return _ok({"messages": summaries}) if summaries else None
    new_history = str(data.get("historyId") or last)
    ids: list[str] = []
    for entry in data.get("history", []) or []:
        for added in entry.get("messagesAdded", []) or []:
            message = added.get("message") or {}
            message_id = str(message.get("id", ""))
            if message_id and message_id not in ids:
                ids.append(message_id)
    if not ids:
        if new_history:
            await ctx.set_sync("history_id", new_history)
        return None
    summaries = await _summaries(ctx, ids)
    if new_history:
        await ctx.set_sync("history_id", new_history)
    return _ok({"messages": summaries}) if summaries else None


def _calendar_id(params: dict[str, Any]) -> str:
    return str(params.get("calendar_id") or "primary").strip() or "primary"


def _calendar_path(params: dict[str, Any], suffix: str = "") -> str:
    return f"/calendars/{quote(_calendar_id(params), safe='')}{suffix}"


def _event_time(value: str, time_zone: str) -> dict[str, str]:
    item = {"dateTime": value}
    if time_zone:
        item["timeZone"] = time_zone
    return item


def _event_range(
    params: dict[str, Any],
) -> tuple[dict[str, str] | None, dict[str, str] | None]:
    start = str(params.get("start") or "").strip()
    end = str(params.get("end") or "").strip()
    if not start or not end:
        return None, None
    if params.get("all_day"):
        return {"date": start}, {"date": end}
    time_zone = str(params.get("time_zone") or "").strip()
    return _event_time(start, time_zone), _event_time(end, time_zone)


def _attendees(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append({"email": item.strip()})
        elif isinstance(item, dict) and str(item.get("email") or "").strip():
            person = {"email": str(item["email"]).strip()}
            for key in ("displayName", "optional", "responseStatus"):
                if key in item:
                    person[key] = item[key]
            out.append(person)
    return out


def _event_view(event: dict[str, Any]) -> dict[str, Any]:
    organizer = event.get("organizer") or {}
    return {
        "id": str(event.get("id", "")),
        "summary": str(event.get("summary", "")),
        "status": str(event.get("status", "")),
        "start": event.get("start") or {},
        "end": event.get("end") or {},
        "location": str(event.get("location", "")),
        "description": str(event.get("description", "")),
        "attendees": event.get("attendees") or [],
        "html_link": str(event.get("htmlLink", "")),
        "organizer": str(organizer.get("email", "")),
        "recurring_event_id": str(event.get("recurringEventId", "")),
    }


async def list_events(ctx: Any, params: dict[str, Any]) -> Any:
    now = datetime.now(UTC)
    time_min = str(params.get("time_min") or now.isoformat())
    time_max = str(params.get("time_max") or (now + timedelta(days=7)).isoformat())
    try:
        max_results = int(params.get("max_results") or 20)
    except (TypeError, ValueError):
        max_results = 20
    max_results = max(1, min(max_results, 100))
    request_params: dict[str, Any] = {
        "timeMin": time_min,
        "timeMax": time_max,
        "maxResults": max_results,
        "singleEvents": "true",
        "orderBy": "startTime",
    }
    query = str(params.get("query") or "").strip()
    if query:
        request_params["q"] = query
    page_token = str(params.get("page_token") or "").strip()
    if page_token:
        request_params["pageToken"] = page_token
    time_zone = str(params.get("time_zone") or "").strip()
    if time_zone:
        request_params["timeZone"] = time_zone
    data = await ctx.json("GET", _calendar_path(params, "/events"), params=request_params)
    return _ok(
        {
            "calendar_id": _calendar_id(params),
            "events": [_event_view(event) for event in data.get("items", []) or []],
            "next_page_token": str(data.get("nextPageToken", "")),
        }
    )


async def create_event(ctx: Any, params: dict[str, Any]) -> Any:
    summary = str(params.get("summary") or "").strip()
    if not summary:
        return _err("an event summary is required")
    start, end = _event_range(params)
    if start is None or end is None:
        return _err("a start and end are required")
    body: dict[str, Any] = {"summary": summary, "start": start, "end": end}
    for key in ("description", "location", "visibility", "transparency"):
        value = str(params.get(key) or "").strip()
        if value:
            body[key] = value
    attendees = _attendees(params.get("attendees"))
    if attendees:
        body["attendees"] = attendees
    data = await ctx.json("POST", _calendar_path(params, "/events"), json=body)
    return _ok({"event": _event_view(data)})


async def update_event(ctx: Any, params: dict[str, Any]) -> Any:
    event_id = str(params.get("event_id") or "").strip()
    if not event_id:
        return _err("an event id is required")
    patch: dict[str, Any] = {}
    for key in ("summary", "description", "location", "visibility", "transparency"):
        if key in params and params[key] is not None:
            patch[key] = str(params[key])
    if "start" in params or "end" in params:
        start, end = _event_range(params)
        if start is None or end is None:
            return _err("both start and end are needed to change the time")
        patch["start"] = start
        patch["end"] = end
    if "attendees" in params:
        patch["attendees"] = _attendees(params.get("attendees"))
    if not patch:
        return _err("nothing to update")
    data = await ctx.json(
        "PATCH",
        _calendar_path(params, f"/events/{quote(event_id, safe='')}"),
        json=patch,
    )
    return _ok({"event": _event_view(data)})


async def delete_event(ctx: Any, params: dict[str, Any]) -> Any:
    event_id = str(params.get("event_id") or "").strip()
    if not event_id:
        return _err("an event id is required")
    await ctx.request(
        "DELETE",
        _calendar_path(params, f"/events/{quote(event_id, safe='')}"),
    )
    return _ok({"deleted": event_id, "calendar_id": _calendar_id(params)})


async def freebusy(ctx: Any, params: dict[str, Any]) -> Any:
    time_min = str(params.get("time_min") or "").strip()
    time_max = str(params.get("time_max") or "").strip()
    if not time_min or not time_max:
        return _err("a start and end time are required")
    calendar_id = _calendar_id(params)
    data = await ctx.json(
        "POST",
        "/freeBusy",
        json={
            "timeMin": time_min,
            "timeMax": time_max,
            "items": [{"id": calendar_id}],
        },
    )
    entry = (data.get("calendars") or {}).get(calendar_id) or {}
    return _ok(
        {
            "calendar_id": calendar_id,
            "busy": entry.get("busy") or [],
            "errors": entry.get("errors") or [],
        }
    )


_TOOL_LIST = ToolDefinition(
    key="list_messages",
    description=(
        "List recent mail and Gmail messages, newest first. Use a Gmail search query "
        "to narrow the list. Returns sender, subject, date, snippet, and message id."
    ),
    effect="read",
    required_scopes=(GMAIL_READ,),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Gmail search query."},
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": "How many messages to return.",
            },
            "page_token": {"type": "string", "description": "Next-page token."},
        },
        "required": [],
    },
    handler=list_messages,
)

_TOOL_GET = ToolDefinition(
    key="get_message",
    description="Read one email message by id, including its plain-text body.",
    effect="read",
    required_scopes=(GMAIL_READ,),
    input_schema={
        "type": "object",
        "properties": {"message_id": {"type": "string"}},
        "required": ["message_id"],
    },
    handler=get_message,
)

_TOOL_SEND = ToolDefinition(
    key="send_email",
    description="Send an email from the connected Gmail account.",
    effect="write",
    required_scopes=(GMAIL_COMPOSE,),
    input_schema={
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Recipient address."},
            "cc": {"type": "string", "description": "Optional cc address."},
            "bcc": {"type": "string", "description": "Optional bcc address."},
            "subject": {"type": "string"},
            "body": {"type": "string", "description": "Plain-text body."},
            "thread_id": {"type": "string", "description": "Optional Gmail thread id."},
        },
        "required": ["to", "subject", "body"],
    },
    handler=send_email,
)

_TOOL_DRAFT = ToolDefinition(
    key="create_draft",
    description="Create an email draft in Gmail without sending it.",
    effect="write",
    required_scopes=(GMAIL_COMPOSE,),
    input_schema={
        "type": "object",
        "properties": {
            "to": {"type": "string"},
            "cc": {"type": "string"},
            "bcc": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "thread_id": {"type": "string"},
        },
        "required": ["to", "subject", "body"],
    },
    handler=create_draft,
)

_TOOL_SEND_DRAFT = ToolDefinition(
    key="send_draft",
    description="Send an existing email draft by id.",
    effect="write",
    required_scopes=(GMAIL_COMPOSE,),
    input_schema={
        "type": "object",
        "properties": {"draft_id": {"type": "string"}},
        "required": ["draft_id"],
    },
    handler=send_draft,
)

_TOOL_PROFILE = ToolDefinition(
    key="get_profile",
    description="Read the connected Gmail account's email address and mailbox totals.",
    effect="read",
    required_scopes=(GMAIL_READ,),
    input_schema={"type": "object", "properties": {}, "required": []},
    handler=get_profile,
)

_TOOL_CAL_LIST = ToolDefinition(
    key="list_events",
    description=(
        "List calendar events, newest first. Defaults to the next 7 days on the "
        "primary calendar. Returns start, end, location, attendees, and event id."
    ),
    effect="read",
    required_scopes=(CALENDAR_READ,),
    input_schema={
        "type": "object",
        "properties": {
            "calendar_id": {
                "type": "string",
                "description": "Calendar id; defaults to primary.",
            },
            "time_min": {
                "type": "string",
                "description": "RFC3339 start of the window; defaults to now.",
            },
            "time_max": {
                "type": "string",
                "description": "RFC3339 end of the window; defaults to 7 days out.",
            },
            "query": {"type": "string", "description": "Free-text search."},
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "How many events to return.",
            },
            "page_token": {"type": "string", "description": "Next-page token."},
            "time_zone": {"type": "string", "description": "IANA time zone."},
        },
        "required": [],
    },
    handler=list_events,
)

_TOOL_CAL_CREATE = ToolDefinition(
    key="create_event",
    description="Schedule or create a calendar event (a meeting) with a start and end.",
    effect="write",
    required_scopes=(CALENDAR_EVENTS,),
    input_schema={
        "type": "object",
        "properties": {
            "calendar_id": {"type": "string", "description": "Calendar id; defaults to primary."},
            "summary": {"type": "string", "description": "Event title."},
            "start": {
                "type": "string",
                "description": "RFC3339 date-time, or YYYY-MM-DD when all_day is true.",
            },
            "end": {
                "type": "string",
                "description": "RFC3339 date-time, or an exclusive YYYY-MM-DD when all_day is true.",
            },
            "all_day": {"type": "boolean", "description": "Use date-only start and end."},
            "time_zone": {"type": "string", "description": "IANA time zone for the event."},
            "description": {"type": "string"},
            "location": {"type": "string"},
            "attendees": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Email addresses to invite.",
            },
            "visibility": {
                "type": "string",
                "description": "default, public, private, or confidential.",
            },
            "transparency": {
                "type": "string",
                "description": "opaque (busy) or transparent (free).",
            },
        },
        "required": ["summary", "start", "end"],
    },
    handler=create_event,
)

_TOOL_CAL_UPDATE = ToolDefinition(
    key="update_event",
    description="Reschedule or change an existing calendar event by id.",
    effect="write",
    required_scopes=(CALENDAR_EVENTS,),
    input_schema={
        "type": "object",
        "properties": {
            "calendar_id": {"type": "string", "description": "Calendar id; defaults to primary."},
            "event_id": {"type": "string"},
            "summary": {"type": "string"},
            "start": {
                "type": "string",
                "description": "RFC3339 date-time, or YYYY-MM-DD when all_day is true.",
            },
            "end": {
                "type": "string",
                "description": "RFC3339 date-time, or an exclusive YYYY-MM-DD when all_day is true.",
            },
            "all_day": {"type": "boolean"},
            "time_zone": {"type": "string"},
            "description": {"type": "string"},
            "location": {"type": "string"},
            "attendees": {"type": "array", "items": {"type": "string"}},
            "visibility": {"type": "string"},
            "transparency": {"type": "string"},
        },
        "required": ["event_id"],
    },
    handler=update_event,
)

_TOOL_CAL_DELETE = ToolDefinition(
    key="delete_event",
    description="Cancel or delete a calendar event by id. This is permanent.",
    effect="destructive",
    required_scopes=(CALENDAR_EVENTS,),
    input_schema={
        "type": "object",
        "properties": {
            "calendar_id": {"type": "string", "description": "Calendar id; defaults to primary."},
            "event_id": {"type": "string"},
        },
        "required": ["event_id"],
    },
    handler=delete_event,
)

_TOOL_CAL_FREEBUSY = ToolDefinition(
    key="freebusy",
    description="Check availability and free/busy times on a calendar between two times.",
    effect="read",
    required_scopes=(CALENDAR_READ,),
    input_schema={
        "type": "object",
        "properties": {
            "calendar_id": {"type": "string", "description": "Calendar id; defaults to primary."},
            "time_min": {"type": "string", "description": "RFC3339 start of the window."},
            "time_max": {"type": "string", "description": "RFC3339 end of the window."},
        },
        "required": ["time_min", "time_max"],
    },
    handler=freebusy,
)

GMAIL_APP = AppDefinition(
    key="gmail",
    name="Gmail",
    provider="google",
    scopes=(GMAIL_READ, GMAIL_COMPOSE),
    tools=(
        _TOOL_LIST,
        _TOOL_GET,
        _TOOL_SEND,
        _TOOL_DRAFT,
        _TOOL_SEND_DRAFT,
        _TOOL_PROFILE,
    ),
    poll=PollDefinition(key="new_mail", every_minutes=5.0, handler=poll_new_mail),
)

CALENDAR_APP = AppDefinition(
    key="calendar",
    name="Google Calendar",
    provider="google",
    api_base=CALENDAR_API,
    scopes=(CALENDAR_READ, CALENDAR_EVENTS),
    tools=(
        _TOOL_CAL_LIST,
        _TOOL_CAL_CREATE,
        _TOOL_CAL_UPDATE,
        _TOOL_CAL_DELETE,
        _TOOL_CAL_FREEBUSY,
    ),
)

GOOGLE_PROVIDER = ProviderDefinition(
    key="google",
    name="Google",
    auth_modes=("oauth",),
    default_auth_mode="oauth",
    authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
    token_endpoint="https://oauth2.googleapis.com/token",
    revocation_endpoint="https://oauth2.googleapis.com/revoke",
    userinfo_endpoint="https://openidconnect.googleapis.com/v1/userinfo",
    api_base="https://gmail.googleapis.com",
    client_id_name="GOOGLE_CLIENT_ID",
    client_secret_name="GOOGLE_CLIENT_SECRET",
    base_scopes=("openid", "email", "profile"),
    extra_auth_params={"access_type": "offline", "include_granted_scopes": "true"},
    setup_url=_GOOGLE_SETUP_URL,
    setup_steps=_GOOGLE_SETUP_STEPS,
    apps=(GMAIL_APP, CALENDAR_APP),
)

PROVIDERS = [GOOGLE_PROVIDER]
