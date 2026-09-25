"""Builds the agent's working context each turn.

Newest-first, bounded, LLM-free: recent memorable events, relationship
notes for the entities those events reference, and the pending-approval
queue. Today's contribution is capped by agent.daily_surface_cap so a
busy morning can't flood the prompt, and the whole thing is bounded by a
character budget before it ever reaches a prompt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..authz.approvals import Approval, Approvals
from ..config import AgentConfig
from .entities import Entities, EntityNote, Sender
from .events import Event, EventStore, start_of_today

# ~4000 tokens of context material at 4 chars/token, split across events
# and notes.
_CONTEXT_BUDGET_CHARS = 16_000


@dataclass
class AgentContext:
    events: list[Event] = field(default_factory=list)
    notes: list[EntityNote] = field(default_factory=list)
    pending_approvals: list[Approval] = field(default_factory=list)
    today_memorable: int = 0


def apply_daily_cap(events: list[Event], cap: int, now: datetime | None = None) -> list[Event]:
    """Keep at most `cap` of today's events, highest salience first; older
    history passes through untouched. Result stays newest-first."""
    now = now or datetime.now(timezone.utc)
    today = [e for e in events if e.occurred_at.date() == now.date()]
    if len(today) <= cap:
        return events
    kept = {id(e) for e in sorted(today, key=lambda e: e.salience_score, reverse=True)[:cap]}
    combined = [e for e in events if e.occurred_at.date() != now.date() or id(e) in kept]
    combined.sort(key=lambda e: e.occurred_at, reverse=True)
    return combined


def bound_events(events: list[Event], budget_chars: int) -> list[Event]:
    """Take newest events until the character budget is spent."""
    used = 0
    out: list[Event] = []
    for event in events:
        size = len(json.dumps(event.payload, default=str))
        if used + size > budget_chars:
            break
        used += size
        out.append(event)
    return out


def collect_senders(events: list[Event]) -> list[Sender]:
    """Every sender referenced anywhere inside the events' payloads."""
    found: list[Sender] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            sender = obj.get("_sender")
            if isinstance(sender, dict) and sender.get("handle"):
                found.append(Sender(str(sender.get("platform", "")), str(sender["handle"])))
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    for event in events:
        walk(event.payload)
    return found


class ContextBuilder:
    def __init__(
        self,
        store: EventStore,
        entities: Entities,
        approvals: Approvals,
        config: AgentConfig,
    ) -> None:
        self._store = store
        self._entities = entities
        self._approvals = approvals
        self._config = config

    async def build(self) -> AgentContext:
        events = await self._store.recent_memorable(limit=50)
        events = apply_daily_cap(events, self._config.daily_surface_cap)
        events = bound_events(events, _CONTEXT_BUDGET_CHARS)

        notes: list[EntityNote] = []
        seen_identities: set[int] = set()
        for sender in collect_senders(events):
            entity = await self._entities.resolve_exact(sender.platform, sender.handle)
            if entity is None or entity.id in seen_identities:
                continue
            seen_identities.add(entity.id)
            notes.extend(await self._entities.notes_for(entity.id, limit=5))
        notes.sort(key=lambda n: n.created_at, reverse=True)
        notes = notes[:10]

        pending = await self._approvals.list_pending()
        today_memorable = await self._store.count_memorable_since(start_of_today())

        return AgentContext(
            events=events,
            notes=notes,
            pending_approvals=pending,
            today_memorable=today_memorable,
        )
