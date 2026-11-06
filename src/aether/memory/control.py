"""Inspection and forgetting — the read view and every destructive action.

One object owns "what do you remember?" and "forget that", so no route or
tool invents its own delete. Forgetting an event is the primary unit: the
sealed row goes, its chunks go, the resident index and the lexical index
drop it in the same breath, any commitment it produced is cancelled, any
claim it supported is superseded, and a tombstone keeps the next poll from
storing the same message again.

The inspection half never reads sealed text into a list and never returns
JSON: each item is one plain sentence built here, so the panel and the model
both render what a person would say. Counts come from the resident index and
from plaintext columns only.

Every action is audited. The row carries ids and counts, never words.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from ..authz.audit import AuditLog
from .commitments import CANCELLED
from .crypto import Cipher
from .events import Event, EventStore
from .facts import Facts
from .indexes import MemoryIndex
from .lexical import LexicalIndex
from .mail import MAIL_KIND, parse_addresses
from .summaries import DAILY, WEEKLY, Summaries

log = logging.getLogger("aether.memory.control")

# the panel's one line per item: long enough to recognise, short enough to read
_LABEL_CHARS = 140


@dataclass
class ForgetReport:
    """What one forget did — `found` false is a quiet no-op, not an error."""

    found: bool
    kind: str = ""
    label: str = ""
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def note(self) -> str:
        return self.label or "Nothing to forget."


def _clip(text: str, limit: int = _LABEL_CHARS) -> str:
    clean = " ".join((text or "").split())
    return clean if len(clean) <= limit else clean[:limit].rstrip() + "…"


def describe_event(
    event: Event | None, *, kind: str = "", payload: dict[str, Any] | None = None
) -> str:
    """One remembered item as a person would say it, never its JSON.

    `kind`/`payload` are the fallback when the event is no longer resident
    (the row is being deleted, so the caller may only hold its decrypted
    payload). A mail names its sender and subject; a message quotes a line;
    a document names its file. Anything else is "a remembered item".
    """
    if event is not None:
        kind = event.kind or kind
        payload = event.payload or payload or {}
    payload = payload or {}
    if kind == MAIL_KIND:
        who = _sender_name(payload.get("sender"))
        subject = _clip(str(payload.get("subject") or ""), 80)
        if who and subject:
            return f"an email from {who} about {subject}"
        if who:
            return f"an email from {who}"
        if subject:
            return f"an email about {subject}"
        return "an email"
    if kind == "chat_message":
        said = _clip(str(payload.get("text") or ""), 80)
        return f"a message saying “{said}”" if said else "a message"
    if kind == "shared_doc":
        name = _clip(str(payload.get("filename") or ""), 80)
        return f"the document {name}" if name else "a shared document"
    if kind == "shared_image":
        name = _clip(str(payload.get("filename") or ""), 80)
        return f"the picture {name}" if name else "a shared picture"
    if kind in ("shared_audio", "shared_video"):
        name = _clip(str(payload.get("filename") or ""), 80)
        noun = "voice note" if kind == "shared_audio" else "video"
        return f"the {noun} {name}" if name else f"a shared {noun}"
    if kind == "screen_capture":
        return "a screen capture"
    summary = _clip(str(payload.get("summary") or ""), 80)
    return f"a remembered item about {summary}" if summary else "a remembered item"


def _sender_name(raw: Any) -> str:
    parsed = parse_addresses(raw)
    if parsed:
        return parsed[0].name or parsed[0].address
    return ""


class MemoryControl:
    """The owner's view of memory, and the only way anything is forgotten."""

    def __init__(
        self,
        *,
        pool: asyncpg.Pool,
        cipher: Cipher,
        audit: AuditLog,
        events: EventStore,
        index: MemoryIndex,
        lexical: LexicalIndex,
        vectors: Any = None,
        facts: Facts | None = None,
        commitments: Any = None,
        summaries: Summaries | None = None,
        watches: Any = None,
        memory: Any = None,
        embedding_model: str = "",
    ) -> None:
        self._pool = pool
        self._cipher = cipher
        self._audit = audit
        self._events = events
        self._index = index
        self._lexical = lexical
        self._vectors = vectors
        self._facts = facts
        self._commitments = commitments
        self._summaries = summaries
        self._watches = watches
        self._memory = memory
        self._embedding_model = embedding_model

    # -- inspection ---------------------------------------------------------

    async def items(self, *, query: str = "", limit: int = 30) -> list[dict[str, Any]]:
        """The remembered items, newest first — or what a search finds.

        A search rides the lexical index (the same one retrieval uses), so the
        panel and the agent agree about what is findable. Each result is one
        plain sentence; the raw id travels only as the row's handle.
        """
        limit = max(1, min(limit, 100))
        found: list[Any] = []
        if query.strip():
            try:
                hits = self._lexical.search(query, limit=limit)
            except Exception:
                log.warning("memory inspection search failed", exc_info=True)
                hits = []
            seen: set[int] = set()
            for hit in hits:
                event = self._index.get(hit.event_id)
                if event is None or event.id in seen:
                    continue
                seen.add(event.id)
                found.append(event)
        else:
            found = self._index.recent(limit)
        return [self._item(event) for event in found]

    async def claims(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """The active claims and open promises, each with its own Forget."""
        out: list[dict[str, Any]] = []
        if self._facts is not None:
            for fact in await self._facts.active(limit=limit):
                who = fact.subject or ("you" if fact.subject_identity_id is not None else "")
                plain = f"{who} — {fact.key}: {fact.value}" if who else f"{fact.key}: {fact.value}"
                out.append({"id": fact.id, "kind": "fact", "plain": _clip(plain)})
        if self._commitments is not None:
            for commitment in await self._commitments.list_open(limit=limit):
                out.append(
                    {
                        "id": commitment.id,
                        "kind": "commitment",
                        "plain": _clip(f"{commitment.kind}: {commitment.text}"),
                    }
                )
        if self._summaries is not None:
            for kind in (DAILY, WEEKLY):
                try:
                    rows = await self._summaries.recent(kind, limit=limit)
                except Exception:
                    rows = []
                for summary in rows:
                    out.append(
                        {
                            "id": summary.id,
                            "kind": "summary",
                            "plain": _clip(f"{kind} summary: {summary.text}"),
                        }
                    )
        return out

    async def overview(self) -> dict[str, Any]:
        """The status line: how much is resident, what is on, how search works."""
        chunks = await self._pool.fetchval("SELECT count(*) FROM memory_chunks")
        facts = 0
        if self._facts is not None:
            try:
                facts = len(await self._facts.active())
            except Exception:
                facts = 0
        commitments = 0
        if self._commitments is not None:
            try:
                commitments = len(await self._commitments.list_open(limit=200))
            except Exception:
                commitments = 0
        watches = 0
        if self._watches is not None:
            try:
                watches = len(await self._watches.list_active())
            except Exception:
                watches = 0
        semantic = bool(getattr(self._vectors, "enabled", False))
        return {
            "resident_events": len(self._index),
            "indexed_chunks": int(chunks or 0),
            "active_facts": facts,
            "open_commitments": commitments,
            "active_watches": watches,
            "semantic_search": {
                "on": semantic,
                "model": self._embedding_model if semantic else "",
                "note": "" if semantic else "Search by meaning is off; keyword search still works.",
            },
            "flags": {
                "enrichment": bool(getattr(self._memory, "enrichment", False)),
                "query_pipeline": bool(getattr(self._memory, "query_pipeline", False)),
                "summaries": bool(getattr(self._memory, "summaries", False)),
            },
        }

    def _item(self, event: Event) -> dict[str, Any]:
        return {
            "id": event.id,
            "kind": event.kind,
            "source": event.source,
            "at": event.occurred_at.isoformat(),
            "plain": describe_event(event),
        }

    # -- forgetting ---------------------------------------------------------

    async def forget_event(self, event_id: int, *, actor: str = "owner") -> ForgetReport:
        """Forget one remembered item and every consequence of it.

        The row is deleted, but only after its chunks, its promises and the
        claims it supported are ended and its pointers are saved — otherwise
        the cascade would take the evidence the audit trail needs.
        """
        row = await self._pool.fetchrow(
            "SELECT id, source_token, dedup_mac FROM events WHERE id = $1", event_id
        )
        if row is None:
            return ForgetReport(found=False)
        event = await self._events.get(event_id)
        label = f"Forgot {describe_event(event)}."

        chunks = 0
        if self._vectors is not None:
            chunks = await self._vectors.remove_event(event_id)
        else:
            chunks = int(
                await self._pool.fetchval(
                    "SELECT count(*) FROM memory_chunks WHERE event_id = $1", event_id
                )
                or 0
            )
        commitments = await self._cancel_commitments(event_id)
        facts = 0
        if self._facts is not None:
            try:
                facts = await self._facts.supersede_for_event(event_id)
            except Exception:
                log.warning("could not end the claims behind event %s", event_id, exc_info=True)
        # the row last: the FK cascade is the backstop for any chunk a race
        # re-created after the explicit delete above
        await self._pool.execute("DELETE FROM events WHERE id = $1", event_id)
        self._index.remove(event_id)
        self._lexical.remove_event(event_id)
        await self._tombstone(row["dedup_mac"], row["source_token"])
        await self._audit_forget(
            "event",
            event_id,
            actor,
            f"forgot one item ({chunks} chunks, {commitments} promises, {facts} claims)",
        )
        return ForgetReport(
            found=True,
            kind="event",
            label=label,
            counts={"chunks": chunks, "commitments": commitments, "facts": facts},
        )

    async def forget_fact(self, fact_id: int, *, actor: str = "owner") -> ForgetReport:
        if self._facts is None:
            return ForgetReport(found=False)
        fact = await self._facts.get(fact_id)
        if fact is None or not await self._facts.forget(fact_id):
            return ForgetReport(found=False)
        await self._audit_forget("fact", fact_id, actor, "forgot one claim")
        return ForgetReport(
            found=True,
            kind="fact",
            label=f"Forgot the claim about {_clip(fact.key, 60)}.",
            counts={"facts": 1},
        )

    async def forget_commitment(self, commitment_id: int, *, actor: str = "owner") -> ForgetReport:
        if self._commitments is None:
            return ForgetReport(found=False)
        commitment = await self._commitments.get(commitment_id)
        if commitment is None:
            return ForgetReport(found=False)
        if not await self._commitments.resolve(commitment_id, CANCELLED):
            return ForgetReport(found=False)
        await self._audit_forget("commitment", commitment_id, actor, "forgot one commitment")
        return ForgetReport(
            found=True,
            kind="commitment",
            label=f"Forgot the promise: {_clip(commitment.text)}",
            counts={"commitments": 1},
        )

    async def forget_summary(self, summary_id: int, *, actor: str = "owner") -> ForgetReport:
        if self._summaries is None:
            return ForgetReport(found=False)
        summary = await self._summaries.by_id(summary_id)
        if summary is None or not await self._summaries.delete(summary_id):
            return ForgetReport(found=False)
        await self._audit_forget("summary", summary_id, actor, "forgot one summary")
        return ForgetReport(
            found=True,
            kind="summary",
            label="Forgot that summary.",
            counts={"summaries": 1},
        )

    # -- internals ----------------------------------------------------------

    async def _cancel_commitments(self, event_id: int) -> int:
        if self._commitments is None:
            return 0
        try:
            rows = await self._pool.fetch(
                "SELECT id FROM commitments WHERE source_event_id = $1 AND status = 'open'",
                event_id,
            )
        except Exception:
            log.warning("could not read commitments for event %s", event_id, exc_info=True)
            return 0
        cancelled = 0
        for row in rows:
            try:
                if await self._commitments.resolve(int(row["id"]), CANCELLED):
                    cancelled += 1
            except Exception:
                log.warning("could not cancel commitment %s", row["id"], exc_info=True)
        return cancelled

    async def _tombstone(self, dedup_mac: Any, source_token: Any) -> None:
        if dedup_mac is None:
            return
        await self._pool.execute(
            "INSERT INTO forgotten_events (dedup_mac, source_token) VALUES ($1, $2)"
            " ON CONFLICT (dedup_mac) DO NOTHING",
            dedup_mac,
            source_token or b"",
        )

    async def _audit_forget(self, kind: str, item_id: int, actor: str, outcome: str) -> None:
        try:
            await self._audit.append(
                actor=actor,
                tool_name="memory.forget",
                decision="allow",
                params={"kind": kind, "id": item_id},
                outcome=outcome,
            )
        except Exception:
            log.warning("could not audit a forget", exc_info=True)
