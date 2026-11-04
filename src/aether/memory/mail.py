"""Mail events: one event per message, with the raw headers kept.

The Gmail poll used to land as a single JSON blob truncated to 4000
characters, so several messages in one poll collided into one row and the
tail was silently cut. A mail is now one event, carrying the raw
From/To/Cc values the provider returned — display names and addresses
intact, never collapsed into the owner's own address (§8) — plus the
body, so the write-time enrichment that follows has a single message to
read.

The payload deliberately keeps the provider's own field names and string
shapes (`sender`, `to`, `cc`), because the Gmail allowlist judges that
same envelope: filtering must keep working unchanged, and it must keep
happening before anything is stored.

Parsing lives here rather than in the provider because it is used by the
whole memory path — enrichment links the addresses to identities, and
rendering shows the name and address back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

MAIL_SOURCE = "gmail"
MAIL_KIND = "mail"

# the payload keeps a bounded copy of the body; the vector layer chunks the
# whole thing, so this is a preview budget, not the storage limit
BODY_CAP = 20_000


@dataclass(frozen=True)
class Address:
    """One display name and address, as the header carried them."""

    name: str
    address: str


def parse_addresses(raw: Any) -> tuple[Address, ...]:
    """`'Ada <ada@x.com>, b@y.com'` -> parsed pairs.

    Stdlib `email.utils`, so the odd shapes real mail carries (a quoted
    comma in a display name, a bare address, a list) come out right
    instead of being split on the first comma by hand.
    """
    parts = raw if isinstance(raw, (list, tuple)) else [raw]
    text = ", ".join(str(part) for part in parts if part)
    if not text.strip():
        return ()
    out: list[Address] = []
    for name, address in getaddresses([text]):
        address = address.strip()
        if not address:
            continue
        out.append(Address(name=name.strip(), address=address))
    return tuple(out)


def parse_sent_at(raw: Any, fallback: datetime) -> datetime:
    """The Date header as an aware UTC time.

    A missing or unreadable Date falls back rather than dropping the
    message — a mail whose clock is wrong is still a mail.
    """
    if isinstance(raw, str) and raw.strip():
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            when = None
        if when is not None:
            return when if when.tzinfo is not None else when.replace(tzinfo=UTC)
    return fallback


@dataclass(frozen=True)
class MailEvent:
    """One message, ready to ingest."""

    message_id: str
    thread_id: str
    payload: dict[str, Any]
    occurred_at: datetime
    sender: str  # the bare from address, for the allowlist and identity linking

    @property
    def dedup_key(self) -> str:
        """The provider's own identity for this message — so the poll and
        the tool path agree, and a refetch is never a second message."""
        return f"{MAIL_SOURCE}:{self.message_id}"


def split_mail(payload: Any, *, now: datetime | None = None) -> list[MailEvent]:
    """A provider poll's envelope -> one `MailEvent` per message.

    Anything that is not a messages envelope yields nothing, so a caller
    keeps its own shape for results this module does not model.
    """
    envelope = payload.get("data") if isinstance(payload, dict) else None
    messages = envelope.get("messages") if isinstance(envelope, dict) else None
    if not isinstance(messages, list):
        return []
    fallback = now or datetime.now(UTC)
    out: list[MailEvent] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        message_id = str(message.get("id") or "").strip()
        if not message_id:
            continue
        occurred_at = parse_sent_at(message.get("date"), fallback)
        addresses = parse_addresses(message.get("sender") or message.get("from"))
        out.append(
            MailEvent(
                message_id=message_id,
                thread_id=str(message.get("thread_id") or ""),
                payload=mail_payload(message),
                occurred_at=occurred_at,
                sender=addresses[0].address if addresses else "",
            )
        )
    return out


def mail_payload(message: dict[str, Any]) -> dict[str, Any]:
    """The event payload for one message.

    The headers stay as the provider handed them (§8 keeps them raw), and
    `sender`/`to`/`cc` keep the Gmail allowlist's own field names and
    string shapes, so the filter judges exactly what it judged before.
    """
    payload: dict[str, Any] = {
        "message_id": str(message.get("id") or ""),
        "thread_id": str(message.get("thread_id") or ""),
        "subject": str(message.get("subject") or ""),
        "sender": _raw(message.get("sender")),
        "to": _raw(message.get("to")),
        "cc": _raw(message.get("cc")),
        "date": str(message.get("date") or ""),
        "snippet": str(message.get("snippet") or ""),
    }
    body = str(message.get("body") or "")
    if body:
        payload["body"] = body[:BODY_CAP]
    label_ids = message.get("label_ids")
    if isinstance(label_ids, list) and label_ids:
        payload["label_ids"] = [str(label) for label in label_ids]
    if message.get("unread"):
        payload["unread"] = True
    return payload


def _raw(value: Any) -> str:
    """A header value as a plain string, whatever shape it arrived in."""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(part) for part in value if part)
    return str(value or "")


def format_address(address: str, name: str, owners: Any = ()) -> str:
    """One rendered address, with "(your other address)" when it is the
    user's own (plan §8).

    The address is always shown as it arrived — never collapsed into the
    owner's primary address and never hidden behind the display name, so a
    mail from a second account still reads as that account.
    """
    addr = (address or "").strip()
    if not addr:
        return ""
    label = f"{name.strip()} <{addr}>" if name and name.strip() else addr
    owned = owners if isinstance(owners, (set, frozenset, list, tuple)) else ()
    if addr.lower() in {str(entry).strip().lower() for entry in owned}:
        label += " (your other address)"
    return label


def _format_list(raw: Any, owners: Any) -> list[str]:
    return [
        rendered
        for rendered in (
            format_address(address.address, address.name, owners)
            for address in parse_addresses(raw)
        )
        if rendered
    ]


def render_mail_event(
    payload: dict[str, Any],
    *,
    occurred_at: Any,
    salience: float = 0.0,
    owners: Any = (),
    body_limit: int = 400,
) -> str:
    """A mail line for the model: who, to whom, when, and the new words.

    Quoted reply history is cut (plan §4.5) so the line leads with what this
    message actually says; the raw headers are kept, and an owner address is
    marked rather than folded into the primary one (§8).
    """
    subject = str(payload.get("subject") or "").strip()
    sender = _format_list(payload.get("sender"), owners)
    to = _format_list(payload.get("to"), owners)
    cc = _format_list(payload.get("cc"), owners)
    body = strip_quoted(str(payload.get("body") or payload.get("snippet") or "")).strip()
    stamp = occurred_at.strftime("%Y-%m-%d %H:%M") if isinstance(occurred_at, datetime) else ""
    head = f"{stamp} {MAIL_SOURCE}/{MAIL_KIND} (salience {salience:.0f})"
    parts: list[str] = []
    if subject:
        parts.append(f"subject: {subject}")
    if sender:
        parts.append("from " + ", ".join(sender))
    if to:
        parts.append("to " + ", ".join(to))
    if cc:
        parts.append("cc " + ", ".join(cc))
    line = f"{head}: " + ("; ".join(parts) if parts else "(no headers)")
    if body:
        line += f" — {body[:body_limit]}"
    return line


# The shapes a quoted reply announces itself with. They are cut from the
# text the index sees (plan §4.5) — a thread's history embedded in every
# later message would otherwise make each reply retrieve as every earlier
# one, and drown the new words.
_QUOTE_STARTS = (
    re.compile(r"^-{2,}\s*original message\s*-{2,}", re.IGNORECASE),
    re.compile(r"^-{2,}\s*forwarded message\s*-{2,}", re.IGNORECASE),
    re.compile(r"^on .{0,120}\bwrote:$", re.IGNORECASE),
    re.compile(r"^on .{0,120}\bat .{0,40}\bwrote:$", re.IGNORECASE),
    re.compile(r"^_{5,}$"),
    re.compile(r"^from:\s", re.IGNORECASE),
    re.compile(r"^sent from my\b", re.IGNORECASE),
)


def strip_quoted(body: str) -> str:
    """A mail body with its quoted replies removed.

    Everything from the first quote marker on is dropped, and every line
    that is itself a quote marker (`> ...`) goes with it. A body that is
    *entirely* a quote keeps its text rather than becoming empty — a reply
    with nothing new in it is still a message that arrived.
    """
    lines = (body or "").splitlines()
    cut = len(lines)
    kept: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(">"):
            cut = min(cut, index)
            break
        if any(pattern.match(stripped) for pattern in _QUOTE_STARTS):
            cut = min(cut, index)
            break
        kept.append(line)
    head = "\n".join(kept if kept else lines[:cut]).strip()
    if head:
        return head
    return (body or "").strip()
