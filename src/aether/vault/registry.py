"""Code-owned token and dedup domains.

A caller names a registered domain; the registry maps it to the category,
version and namespace that derive the subkey. Callers never choose a raw key
or namespace, and an unregistered domain is a programming error rather than an
implicit new domain.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import VaultProtocolError
from .schema import CATEGORIES


@dataclass(frozen=True)
class Domain:
    cat: str
    ver: int
    namespace: str


def _domain(cat: str, namespace: str, ver: int = 1) -> Domain:
    if cat not in CATEGORIES:
        raise ValueError(f"unknown category {cat!r}")
    return Domain(cat=cat, ver=ver, namespace=namespace)


# Equality lookup tokens. The namespace is the declared join field, so a token
# for an event source cannot be compared with a token for a chat surface.
TOKEN_DOMAINS: dict[str, Domain] = {
    "identity.handle": _domain("memory", "identity_handles.normalized"),
    "event.source": _domain("memory", "events.source"),
    "chat.surface": _domain("memory", "chat_messages.surface"),
    "fact.subject": _domain("memory", "facts.subject"),
    "summary.period": _domain("memory", "summaries.period"),
    "focus.chat": _domain("memory", "focus.chat_ref"),
    "conclusion.key": _domain("memory", "conclusions.key"),
    "inbound.chat": _domain("operations", "inbound_messages.chat"),
    "inbound.message": _domain("operations", "inbound_messages.message"),
    "inbound.key": _domain("operations", "inbound_messages.key"),
    "alert.scope": _domain("operations", "alerts.scope"),
    "delivery.surface": _domain("operations", "deliveries.surface"),
    "proactive.day": _domain("operations", "proactive_tasks.day"),
    "proactive.opportunity": _domain("operations", "proactive_tasks.opportunity"),
    "media.origin": _domain("media", "media_blobs.origin"),
    "workspace.name": _domain("workspace", "workspace_files.name"),
}

# Content dedup tags. These are separate from lookup tokens: the tag is only
# used for equality and is never a stored lookup key for a user value.
DEDUP_DOMAINS: dict[str, Domain] = {
    "event.dedup": _domain("memory", "events.dedup"),
    "media.content": _domain("media", "media_blobs.dedup"),
}


def resolve_token(domain: str) -> Domain:
    try:
        return TOKEN_DOMAINS[domain]
    except KeyError as exc:
        raise VaultProtocolError("bad_field", f"unregistered token domain {domain!r}") from exc


def resolve_dedup(domain: str) -> Domain:
    try:
        return DEDUP_DOMAINS[domain]
    except KeyError as exc:
        raise VaultProtocolError("bad_field", f"unregistered dedup domain {domain!r}") from exc
