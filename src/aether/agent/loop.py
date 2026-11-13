"""The continuous loop: watch feeds, remember, reason, act — with a human
in the path for anything risky.

One background task. Each tick it (a) fires due MCP source polls (the
user's config.yaml poll_tools — a standing instruction, so these run
directly and are audited), (b) turns inbound chat into memory, (c) scores
everything new for salience, (d) checks the user's standing routines
against the new events — deterministic matching, with the taught action
passing the gate at fire time — and (e) if anything actually happened,
runs one authz-gated LLM turn and fans the reply out to every enabled
surface.

Between ticks it sleeps `agent.tick_seconds`, waking early the moment work
arrives (a message, a screen capture, a wake() from the API).

The executor is the single choke point: every ToolCall the model proposes,
every scheduled action when it fires, and every routine when it triggers,
goes through `classify` → run / park / deny, and lands in the hash-chained
audit log either way. And every act leaves a trace — what triggered it,
what the gate ruled, what came back — so "why did you do that?" always
has a record to answer from.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..alerts.engine import AlertEngine
from ..authz.approvals import APPROVED, DENIED, Approval, Approvals
from ..authz.audit import AuditLog, verification_text
from ..authz.policy import Decision, Policy, Ruling, params_blob
from ..config import AppConfig, PollTool, is_legacy_composio_server
from ..config_store import ConfigManager
from ..config_store import actions_word as _actions_word
from ..connectors.attachments import AttachmentProcessor, attachments_note
from ..connectors.base import (
    Attachment,
    ConnectorUnavailableError,
    InboundMessage,
    MessagingConnector,
    UnknownToolError,
)
from ..connectors.mail_allowlist import MailAllowlist
from ..connectors.mcp_host import MCPHost
from ..connectors.polling import next_poll_delay
from ..connectors.registry import ToolRegistry
from ..llm.agent import run_tool_loop
from ..llm.base import Provider
from ..llm.base import ProviderError as LLMProviderError
from ..llm.failures import ProviderFailure, classify_provider_failure
from ..llm.registry import ProviderRegistry
from ..llm.types import PLAIN_PARAM, Message, ToolCall, ToolResult, WireSpec
from ..memory.conclusions import Conclusions
from ..memory.context import ContextBuilder
from ..memory.deliveries import FAILED, Deliveries
from ..memory.entities import Entities, Sender
from ..memory.events import Event, EventStore
from ..memory.focus import Focus
from ..memory.inbound import MemoryInboundStates
from ..memory.mail import MAIL_KIND, MAIL_SOURCE, render_mail_event, split_mail
from ..memory.pipeline import QueryPipeline
from ..memory.salience import Salience
from ..memory.timewindow import in_quiet_window as _in_quiet_window
from ..memory.timewindow import local_clock as _local_clock
from ..memory.timewindow import parse_quiet_hours as _parse_quiet_hours
from ..memory.vectors import MemoryHit, VectorMemory
from ..memory.verify import find_findings, unsupported
from ..memory.views import Views, wants_attention
from ..message_catalog import render as render_message
from ..origin import (
    alert_action,
    approved_call,
    owner_message,
    reset_origin,
    scheduled,
    set_origin,
    unprompted,
    use_origin,
)
from ..outbound import clean_outbound, is_deliverable
from ..providers.runtime import ProviderRuntime
from ..providers.types import ConnectResult, ProviderError, ProviderNotConfigured
from ..restart import pending as restart_pending
from ..scheduler.jobs import ScheduledAction, Scheduler
from ..secret_env import EnvResolver
from ..secret_store import SecretStore
from ..workspace import SecretRejected, WorkspaceWriteError
from .apps_wizard import AppsWizard
from .composer import compose_answer, deterministic_compose, unresolved
from .config_wizard import ConfigWizard
from .config_wizard import coerce_config_value as _coerce_config_value
from .leaks import LeakGuard, collect_internal_values, strip_values
from .outcomes import (
    OUTCOME_SUMMARIES,
    TERMINAL_TOOLS,
    Ask,
    Notify,
    Reply,
    canonical_tool,
    is_terminal_tool,
    outcome_from_calls,
    outcome_specs,
    record_outcome,
    render_ask,
)
from .prompts import SYSTEM_PROMPT
from .settings import AgentSettings
from .tools import (
    EMPTY_REPLY,
    FAILURE_REPLY,
    TIMEOUT_REPLY,
    describe_ask,
    describe_call,
    describe_dismissed,
    describe_outcome,
    plain_outcome,
    plain_replay,
)
from .traces import ALERT, CARRY_OUT, SCHEDULED, TURN, Traces
from .triage import TriageDecision, triage_unprompted

log = logging.getLogger("aether.agent")

_MAX_OBSERVATIONS = 20

# A solicited turn waits this long for write-time enrichment still in
# flight before it answers from the deterministic floor. The classifier is
# one cheap model turn per event, so a burst can outrun the question that
# followed it; this is the bound, not a promise to wait for every event.
ENRICHMENT_WAIT_SECONDS = 2.0

# History windowing (token control): a turn's prompt is capped at this many
# messages — the context block and the newest triggers always stay, the
# oldest middle entries fall off first. The tool loop's own back-and-forth
# appends after this, so it isn't truncated mid-flight.
_MAX_HISTORY_MESSAGES = 12
# ~4 chars/token; 12k chars ≈ 3k tokens of trigger text per call
_MAX_HISTORY_CHARS = 12_000

# how much of the cross-surface transcript (chat_messages, both directions)
# is offered to each turn as conversation memory. The window above trims it
# oldest-first under pressure — events and the fresh inbound always survive.
_TRANSCRIPT_MESSAGES = 20

# Telegram's own "typing…" marker fades after ~5 seconds, so a turn that
# runs longer refreshes it at this cadence — one fire at turn start would
# go dark while the work is still honest.
TYPING_REFRESH_SECONDS = 4.5

# A turn that dies on a provider outage is tried again by the loop itself:
# the first failure tells the user the cause and the wait, later attempts
# stay quiet until one lands. Bounded so a provider that never comes back
# ends with one plain sentence instead of retrying forever.
MAX_TURN_ATTEMPTS = 5
# At most this many unanswered asks are remembered at once — a person can
# queue several questions during an outage, not an unbounded pile.
MAX_PENDING_RESUMES = 10

# A message still 'received' after this long is never auto-answered: it gets
# at most one "still want this?" check and is marked expired, so a stale
# question cannot be answered minutes later as if it were fresh.
PENDING_MAX_AGE_SECONDS = 600.0

# An answer that took at least this long is quoted to the question that
# prompted it, so a slow reply is never mistaken for a comment on whatever
# arrived in between. A prompt answer to the latest message is never quoted.
REPLY_DELAY_SECONDS = 90.0


# the instruction behind the one reply a decision gets: a single exchange
# with no tools, so the answer can only ever be words — the model cannot
# turn an acknowledgment into a retry or a new proposal. The ask rides
# exactly as the user read it on the card, so the reply speaks about the
# same request they decided on.
def _decision_ack_instruction(ask: str, outcome: str) -> str:
    return (
        "The user just decided on an action you proposed. Send one short "
        "plain reply in your own voice — it is the only message you send "
        "about this, and you cannot run or propose anything now, so never "
        "promise a further step.\n\n"
        f"The request, exactly as the user read it:\n\n{ask}\n\n"
        f"What happened:\n{outcome}"
    )


def _window_history(messages: list[Message], keep_head: int = 1) -> list[Message]:
    """Bound a turn's input history so repeated LLM calls stay cheap.

    Pins the first `keep_head` messages (the context block, when one was
    prepended — without one, nothing is pinned, or a stale event would
    survive every window) plus the newest messages; drops oldest middle
    entries until both the count and the rough char budget fit."""
    if len(messages) <= _MAX_HISTORY_MESSAGES:
        windowed = list(messages)
    else:
        windowed = messages[:keep_head] + messages[-(_MAX_HISTORY_MESSAGES - keep_head) :]

    def size(ms: list[Message]) -> int:
        return sum(len(m.text) for m in ms)

    while len(windowed) > keep_head + 1 and size(windowed) > _MAX_HISTORY_CHARS:
        del windowed[keep_head]  # oldest after the pinned head
    return windowed


# the words that make a reply a question about the message it answers —
# anything else replies normally and the model handles it
WHY_REPLY_WORDS = frozenset({"why", "explain"})

# consecutive failed polls before a connector is announced as down. The user
# hears about the state once, and once again when it recovers — never per
# failure. A server with no configured poll has no heartbeat to watch.
POLL_FAILURES_BEFORE_DOWN = 2

# A proactive reply that matches one already sent to this chat inside this
# window, or that mostly echoes a question already closed, is the stale-
# replay bug — it is blocked and logged rather than delivered.
_PROACTIVE_DUPLICATE_HOURS = 6.0
_REPLY_WORDS = re.compile(r"[a-z0-9]+")
_REPLY_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "but",
        "for",
        "with",
        "from",
        "was",
        "were",
        "been",
        "its",
        "this",
        "that",
        "these",
        "those",
        "you",
        "your",
        "his",
        "her",
        "their",
        "them",
        "they",
        "are",
        "not",
        "yes",
        "did",
        "does",
        "have",
        "has",
        "had",
        "will",
        "would",
        "can",
        "could",
        "should",
        "about",
        "out",
        "just",
        "now",
        "here",
        "there",
        "what",
        "when",
        "which",
        "who",
        "how",
        "why",
        "all",
        "any",
        "some",
        "one",
        "two",
        "still",
        "answer",
        "answered",
        "message",
        "messages",
        "please",
    }
)


def _significant_tokens(text: str) -> set[str]:
    """The content words of a sentence — short words and stopwords dropped,
    so a replay check compares meaning, not filler."""
    words = {word for word in _REPLY_WORDS.findall((text or "").lower()) if len(word) >= 3}
    return words - _REPLY_STOPWORDS


# which .env names each messaging surface needs — /apps says "no token in
# .env yet" until every one of them is there
_PLATFORM_TOKENS = {
    "telegram": ("TELEGRAM_BOT_TOKEN",),
    "discord": ("DISCORD_BOT_TOKEN",),
}


_ATTACHMENT_KINDS = frozenset({"shared_doc", "shared_image", "shared_audio", "shared_video"})


def _newest_attachment(observations: list[Event]) -> dict[str, Any] | None:
    """The newest file this turn saw, for the focus object's attachment slot."""
    for event in reversed(observations):
        if event.kind not in _ATTACHMENT_KINDS or not isinstance(event.payload, dict):
            continue
        media_id = event.payload.get("media_id")
        if media_id is None:
            continue
        return {
            "media_id": media_id,
            "filename": str(event.payload.get("filename") or ""),
            "event_id": event.id,
        }
    return None


def _event_search_text(event: Event) -> str:
    """The words an unprompted tick's observation contributes to routing —
    the same meaningful fields the chunker reads, never a JSON dump."""
    payload = event.payload if isinstance(event.payload, dict) else {}
    for key in ("text", "body", "snippet", "subject", "description", "caption"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:600]
    return ""


class CaptureRequestBox:
    """A one-slot mailbox: the agent asks for a screenshot, the companion
    (polling GET /api/screen-request) takes the request and captures."""

    def __init__(self) -> None:
        self._note: str | None = None

    def set(self, note: str = "") -> None:
        self._note = note.strip()

    def take(self) -> str | None:
        note = self._note
        self._note = None
        return note


class SurfaceFanout:
    """Everything the agent says reaches every enabled surface. One dying
    surface never takes the loop down with it."""

    def __init__(
        self,
        connectors: list[MessagingConnector] | None = None,
        hub: Any = None,
        history: Any = None,
        deliveries: Deliveries | None = None,
    ) -> None:
        self.connectors = list(connectors or [])
        self.hub = hub
        self.history = history
        self.deliveries = deliveries

    def attach_deliveries(self, deliveries: Deliveries | None) -> None:
        """Join the delivery record to the fanout after boot builds the
        stores — the same late-wire shape the enrichment pieces use."""
        self.deliveries = deliveries

    def add_connectors(self, connectors: list[MessagingConnector]) -> None:
        self.connectors.extend(connectors)

    def remove_connector(self, name: str) -> MessagingConnector | None:
        """Take one connector out of the fanout by name — a surface the
        user just switched off. Stopping it is the caller's job; this stays
        sync like the rest of the fanout."""
        for i, connector in enumerate(self.connectors):
            if connector.name == name:
                del self.connectors[i]
                return connector
        return None

    async def send_to_user(
        self,
        text: str,
        *,
        attachment: Attachment | None = None,
        record: bool = True,
        outcomes: dict[str, bool] | None = None,
        only: set[str] | None = None,
        reply_to: str | None = None,
    ) -> dict[str, str]:
        """Fan out to every enabled surface, collecting where the words
        landed: {connector name: platform message id}. Those ids are what
        link a user's later "why?" back to the trace of this send.

        An empty message is refused before anything is sent or stored.
        Every surface attempt is recorded — queued, then sent with its
        message id or failed with its error sealed — so a failure is a fact
        the next turn can be told, not a swallowed log line. `record=False`
        is for the retry path, which already owns a delivery row and must not
        mint a new one for the same words; `outcomes`, when given, receives
        each surface's true result so a caller can tell "landed on the web
        but nowhere else" from "landed nowhere". `only`, when given, is one
        surface to narrow the words to — no other surface receives them.
        `reply_to`, when given, is the platform message id a chat
        surface should quote — None means a plain message. The hub has no
        id; a dying surface never takes the loop down. Media rides to the
        chat connectors only — the hub and the transcript stay text-only;
        the attachment is passed only when one is present, so connectors
        and test doubles that never send files see no new kwarg."""
        if not is_deliverable(text):
            return {}
        refs: dict[str, str] = {}
        landed: list[str] = []
        media = {"attachment": attachment} if attachment is not None else {}
        if self.hub is not None and (only is None or "web" in only):
            record_row = await self._record_delivery("web", text) if record else None
            try:
                await self.hub.broadcast(text)
            except Exception as exc:
                log.exception("web chat broadcast failed")
                await self._mark_delivery(record_row, error=str(exc))
                if outcomes is not None:
                    outcomes["web"] = False
            else:
                await self._mark_delivery(record_row, message_id="")
                landed.append("web")
                if outcomes is not None:
                    outcomes["web"] = True
        for connector in self.connectors:
            if only is not None and connector.name not in only:
                continue
            record_row = await self._record_delivery(connector.name, text) if record else None
            try:
                ref = await connector.send_to_user(text, reply_to=reply_to, **media)
            except Exception as exc:
                log.exception("%s send failed — continuing", connector.name)
                await self._mark_delivery(record_row, error=str(exc))
                if outcomes is not None:
                    outcomes[connector.name] = False
                continue
            await self._mark_delivery(record_row, message_id=ref or "")
            landed.append(connector.name)
            if outcomes is not None:
                outcomes[connector.name] = True
            if ref:
                refs[connector.name] = ref
        # everything that actually landed joins the transcript so the next
        # turn can see its own words; the hub persists the web line itself
        # (broadcast), so only connector surfaces are written here
        if self.history is not None:
            for name in landed:
                if name == "web":
                    continue
                try:
                    await self.history.append(name, "out", text)
                except Exception:
                    log.exception("transcript write failed for %s — continuing", name)
        return refs

    async def _record_delivery(self, surface: str, text: str) -> Any:
        if self.deliveries is None:
            return None
        try:
            return await self.deliveries.record_queued(surface, text)
        except Exception:
            log.warning("could not open a delivery record for %s", surface)
            return None

    async def _mark_delivery(self, record: Any, *, message_id: str = "", error: str = "") -> None:
        if record is None or self.deliveries is None:
            return
        try:
            if error:
                await self.deliveries.mark_failed(record.id, error)
            else:
                await self.deliveries.mark_sent(record.id, message_id)
        except Exception:
            log.warning("could not close delivery record %s", getattr(record, "id", "?"))

    async def present_approval(
        self, approval_id: int, text: str, *, attachment: Attachment | None = None
    ) -> None:
        """One plain-language question to every surface — "Send the email
        (to rahul@gmail.com)?" Raw tool names and argument dumps never
        reach the user; the decision record keeps the exact call. A
        media-carrying ask shows the media itself, passed only when
        present."""
        if not is_deliverable(text):
            return
        media = {"attachment": attachment} if attachment is not None else {}
        for connector in self.connectors:
            try:
                await connector.present_approval(approval_id, text, **media)
            except Exception:
                log.exception("%s approval presentation failed", connector.name)
        if not self.connectors and self.hub is None:
            log.warning("approval #%d has no surface to appear on", approval_id)

    async def present_result(
        self,
        task_id: int,
        text: str,
        options: Sequence[str] = (),
        *,
        attachment: Attachment | None = None,
    ) -> None:
        """A finished background task's result, with the one-tap verdicts
        that apply to it. The web chat gets the words like any other
        unprompted note; the chat surfaces get them with the buttons
        attached. The verdicts themselves are decided in the worker, which
        owns the task row — this fanout only carries them. Media rides to
        the chat connectors only, passed only when present."""
        if not is_deliverable(text):
            return
        media = {"attachment": attachment} if attachment is not None else {}
        if self.hub is not None:
            try:
                await self.hub.broadcast(text)
            except Exception:
                log.exception("web chat broadcast failed")
        for connector in self.connectors:
            try:
                await connector.present_result(task_id, text, options, **media)
            except Exception:
                log.exception("%s result presentation failed", connector.name)
        if not self.connectors and self.hub is None:
            log.warning("task #%d result has no surface to appear on", task_id)

    async def show_typing(self) -> None:
        """The "typing…" marker on every surface that has one — the
        platform's own way of saying work is happening instead of silence.
        Cosmetic: a surface without the method is skipped, a failing one is
        a warning, and nothing here is allowed to cost the turn."""
        for connector in self.connectors:
            show = getattr(connector, "show_typing", None)
            if show is None:
                continue
            try:
                await show()
            except Exception:
                log.warning("%s typing indicator failed — continuing", connector.name)


def _settled_key(tool: str, params: Any) -> str:
    """A stable identity for 'this exact tool call' — same tool name and
    JSON-equivalent parameters, independent of dict key order. Two calls to
    the same tool with meaningfully different arguments (a different
    recipient, say) must never collide here; reuses the same canonical
    serialization the authz policy's param-pattern matching is built on,
    so nested values and non-JSON types are handled the same safe way."""
    return f"{tool}\x00{params_blob(params if isinstance(params, dict) else {})}"


# the one answer both sending tools give when words actually went out —
# every refusal answers with different words, so delivery is detected by
# the handler's own note, never by the text the model proposed (a refused
# share_media still carries text in its params; only its result says
# whether anything left the building)
_DELIVERY_NOTE = "Sent to the user's chat surfaces."


def _delivered_by_tool(calls: list[dict[str, Any]], tools: ToolRegistry | None = None) -> bool:
    """True when the turn already put words in front of the user through
    a sending tool — a media share, or a manually registered send helper —
    a send that actually delivered, not one that was denied, parked, or
    refused. A terminal ``message__send`` is not counted here: its outcome
    is parsed separately and delivered by the loop's own path."""
    for call in calls:
        name = call.get("name")
        if not isinstance(name, str):
            continue
        capability = ""
        if tools is not None:
            tool = tools.get(name)
            capability = tool.spec.capability if tool is not None else ""
        if capability not in ("message", "media") and name not in (
            "send_chat_message",
            "share_media",
        ):
            continue
        if call.get("decision") != "allow" or call.get("is_error"):
            continue
        if call.get("result") == _DELIVERY_NOTE:
            return True
    return False


def _parked_this_turn(calls: list[dict[str, Any]]) -> bool:
    """True when the turn ended with an approval card in front of the
    user — the card is the turn's one terminal outcome; any final reply
    beside it reads as filler ("OK.") or as a duplicate of the ask."""
    return any(call.get("approval_id") is not None for call in calls)


@dataclass
class _Resume:
    """One solicited turn waiting out a provider outage.

    The tool loop's own history is kept, not rebuilt: a resumed attempt
    continues exactly where the dead one stopped, so a tool that already
    ran is never proposed and executed a second time.
    """

    messages: list[InboundMessage]
    observations: list[Event]
    history: list[Message]
    calls: list[dict[str, Any]]
    turn_ref: str
    failure: ProviderFailure
    attempts: int
    due_at: datetime
    records: list[Any] | None = None


# the value sentinel: "not on the line" — distinct from None, which is an
# explicit clear (llm.vision_model, agent.quiet_hours)
_MISSING = object()

_CONFIG_USAGE = (
    "Aether /config — the guided walk: send just /config and answer the questions.\n"
    "Everything below is the one-line way to do the same.\n\n"
    "/config show — the overview, with everything changed from chat marked\n"
    "/config show <section|path> — one section's settings, or one value\n"
    "/config set <path> <value> — e.g. /config set agent.tick_seconds 10\n"
    "/config add <path> <value…> — contacts.allowlist <platform> <handle> · "
    "authz.rules <pattern> <decision> [note] · mcp_servers {json object}\n"
    "/config remove <path> <value…> — allowlist [platform] <handle> · "
    "authz.rules <pattern> · mcp_servers <name>\n"
    "/config reset <path> — back to config.yaml's value\n\n"
    "Personal tuning (agent.*, salience.*, llm.*) applies right away; llm.* "
    "restarts me to load it. Security sections (contacts, authz, messaging, "
    "mcp_servers) wait for your one-tap approval. Keys paste in /apps — "
    "/config never sets one."
)


def _parse_config_command(text: str) -> tuple[str, str | None, Any]:
    """(op, path, value) from a /config line; op "usage" for anything the
    grammar doesn't recognize. Deliberately dumb — every meaning lives in
    ConfigManager, so the command path and the tool path share one
    interpretation and can never drift."""
    parts = text.split(maxsplit=2)
    if len(parts) == 1:
        # bare /config — the guided walk when the manager is wired; the
        # parser still reads it as the overview for the unwired answer
        return "show", None, _MISSING
    sub = parts[1].lower()
    if sub == "show":
        shown = parts[2].strip() or None if len(parts) > 2 else None
        return "show", shown, _MISSING
    if sub not in ("set", "add", "remove", "reset"):
        return "usage", None, _MISSING
    if len(parts) < 3:
        return sub, None, _MISSING
    rest = parts[2].strip()
    path, _, tail = rest.partition(" ")
    if not path:
        return "usage", None, _MISSING
    if sub == "reset":
        return "reset", path, _MISSING
    tail = tail.strip()
    if not tail:
        return sub, path, _MISSING
    return sub, path, _coerce_config_value(tail)


class AgentLoop:
    def __init__(
        self,
        *,
        providers: ProviderRegistry | None,
        tools: ToolRegistry,
        policy: Policy,
        approvals: Approvals,
        audit: AuditLog,
        events: EventStore,
        salience: Salience,
        context: ContextBuilder,
        scheduler: Scheduler,
        surfaces: SurfaceFanout,
        capture_box: CaptureRequestBox,
        config: AppConfig,
        host: MCPHost | None = None,
        traces: Traces | None = None,
        agent_settings: AgentSettings | None = None,
        config_manager: ConfigManager | None = None,
        workspace: Any = None,
        resolver: EnvResolver | None = None,
        secret_store: SecretStore | None = None,
        settings: Any = None,
        provider_runtime: ProviderRuntime | None = None,
        transcript: Any = None,
        attachments: AttachmentProcessor | None = None,
        vectors: VectorMemory | None = None,
        mail_filter: MailAllowlist | None = None,
        media: Any = None,
        entities: Entities | None = None,
        views: Views | None = None,
        focus: Focus | None = None,
        conclusions: Conclusions | None = None,
        deliveries: Deliveries | None = None,
        inbound_states: Any = None,
        query_pipeline: QueryPipeline | None = None,
        proactive_path: Any = None,
        summary_job: Any = None,
    ) -> None:
        self._providers = providers
        # the in-flight reply to a decision, if the model is writing one —
        # tests await it; a live loop just lets it land
        self._decision_reply: asyncio.Task[None] | None = None
        self._tools = tools
        self._policy = policy
        self._approvals = approvals
        self._audit = audit
        self._events = events
        self._salience = salience
        self._context = context
        self._scheduler = scheduler
        self._surfaces = surfaces
        self._capture_box = capture_box
        self._config = config
        self._host = host
        self._traces = traces
        self._agent_settings = agent_settings
        self._config_manager = config_manager
        self._workspace = workspace
        self._resolver = resolver
        self._secret_store = secret_store
        self._settings = settings
        self._provider_runtime = provider_runtime
        # the cross-surface transcript (chat_messages) — both directions,
        # read back each turn as conversation memory
        self._transcript = transcript
        # shared files: images described, documents read — past the
        # allowlist, in the drain, so a filtered sender's files are never seen
        self._attachments = attachments
        # the vector half of memory: chunked, embedded, sourced — retrieval
        # is semantic and automatic; a missing embedder means no recall,
        # and memory_search says the plain failure message
        self._vectors = vectors
        # the gmail allowlist, filtering bulk reads the poll path takes
        # straight off the host — bypassing the registry's choke point
        self._mail_filter = mail_filter
        # the encrypted media store — read-only here: a parked call that
        # carries a stored media id shows the media itself with the ask,
        # and a blob that's gone (evicted, purged) degrades to the words
        self._media = media
        # identities — used to claim the owner's own address when a provider
        # connects, so "my other address" renders correctly from the first mail
        self._entities = entities
        # the structured views over the resident index, the per-chat focus
        # object and the live-state conclusions
        self._views = views
        self._focus = focus
        self._conclusions = conclusions
        # the routed query pipeline (routing, lexical + vector fusion, gate,
        # packing). Behind memory.query_pipeline; without it the old vector
        # recall above still serves each turn.
        self._query_pipeline = query_pipeline
        # commitment reminders: due leads the path reports and the model
        # words. Late-wired like the enrichment pieces, because a reminder
        # delivers through this loop's own interruption budget.
        self._proactive_path = proactive_path
        # alerts: deterministic matching and delivery. Late-wired for the
        # same reason — a notification rides the surfaces, and a folded
        # routine action runs through this loop's gate.
        self._alerts: AlertEngine | None = None
        # the summary generator: one due thread/daily/weekly summary per tick,
        # a model call behind memory.summaries
        self._summary_job = summary_job
        # the outbound-send record: a failed or empty send is a fact, not a
        # log line, and the next turn is told about it
        self._deliveries = deliveries
        # the inbound ledger: one terminal outcome per message the user
        # sent, so no later turn re-answers a question that already had its
        # turn. The in-memory ledger is the default; the boot wires the
        # sealed database one.
        self._inbound_states = inbound_states or MemoryInboundStates()
        # the code-only re-delivery lines for the turn ahead (never a model
        # observation) — collected by the tick, read by the turn
        self._delivery_lines: list[str] = []
        # the owner's own addresses, refreshed once per turn so mail renders
        # "(your other address)" without an async lookup per event line
        self._owner_addresses: set[str] = set()
        # the guided /config walk in progress, if any — memory only
        self._wizard: ConfigWizard | None = None
        # the guided /apps walk in progress, if any — same rules, memory only
        self._apps_walk: AppsWizard | None = None
        # the pending profile entries a batched confirmation listed — the
        # ids armed for the next "yes"; memory only, gone on the next message
        self._profile_confirm: list[str] | None = None
        # mcp servers whose "I'll tell you the moment it's up" was already
        # said this boot — a flapping server never announces twice
        self._announced_ready: set[str] = set()
        # the tool_version each server's namespace was last synced at — an
        # MCP server can grow or shrink at runtime, and this is how a tick
        # notices without diffing full tool lists every time
        self._synced_tool_versions: dict[str, int] = {}
        # salience scorers moved off the reply path — same tracking (a
        # completed task is never garbage-collected mid-flight), and this
        # set is what stop() waits out
        self._scoring_tasks: set[asyncio.Task[None]] = set()
        # the summary generator's in-flight pass — at most one at a time
        self._summary_task: asyncio.Task[None] | None = None

        self._queue: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._woken = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        # the vector backfill loop's task — None until start() arms it
        self._backfill_task: asyncio.Task[None] | None = None
        self._running = False
        self._last_event_id = 0
        self._poll_targets: dict[str, tuple[str, PollTool]] = {}
        self._next_poll: dict[str, datetime] = {}
        self._poll_failures: dict[str, int] = {}
        self._down: set[str] = set()
        self._quiet = _parse_quiet_hours(config.agent.quiet_hours)
        self._held: list[str] = []  # unprompted notes waiting out the window
        # solicited turns a provider outage interrupted, waiting their turn
        # again — see _handle_provider_failure and _run_due_resumes
        self._resumes: list[_Resume] = []
        self.rebuild_poll_targets()

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._running = True
            self._task = asyncio.create_task(self.run(), name="agent-loop")
            # catch-up indexing runs beside the answering loop, never on
            # it — a turn never waits on history draining
            if self._vectors is not None and self._vectors.enabled:
                self._backfill_task = asyncio.create_task(
                    self._backfill_loop(), name="vector-backfill"
                )

    async def stop(self) -> None:
        self._running = False
        task, self._task = self._task, None
        backfill, self._backfill_task = self._backfill_task, None
        self._woken.set()
        if backfill is not None:
            backfill.cancel()
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(backfill, timeout=10)
        if task is not None:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=10)
        # scorers a recent tick spawned get the same bounded grace — a tick
        # that ingested an event owes it a score, and nothing retries
        # salience, so they are waited out rather than cancelled
        if self._scoring_tasks:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait(set(self._scoring_tasks), timeout=10)
        # a summary pass in flight gets the same bounded grace
        summary, self._summary_task = self._summary_task, None
        if summary is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait({summary}, timeout=10)

    async def _backfill_loop(self) -> None:
        """Index the events that predate vector memory (or lost a race with
        a rate limit), 50 oldest at a time: tight passes while history is
        draining, a minute's rest once caught up. A failed pass just
        retries — indexed_at is the ledger, so nothing is ever lost."""
        assert self._vectors is not None
        while self._running:
            try:
                handled = await self._vectors.backfill(50)
            except Exception:
                log.exception("vector backfill pass failed — pausing before the retry")
                handled = None
            await asyncio.sleep(1 if handled else 60)

    def notify(self) -> None:
        """Wake the loop early — used after screen captures, poll results,
        anything the API layer knows the agent will want to see now."""
        self._woken.set()

    def submit_message(self, message: InboundMessage) -> None:
        """Queue an inbound chat message — sync, for the WebSocket path and
        tests. Connectors get the awaitable handle_inbound instead, because
        the InboundHandler contract they call through is async."""
        self._queue.put_nowait(message)
        self._woken.set()

    async def handle_inbound(self, message: InboundMessage) -> None:
        """Connector-facing inbound entry — satisfies the async InboundHandler
        contract (connectors await their handler; a sync return is the
        "'NoneType' object can't be awaited" crash)."""
        self.submit_message(message)

    # -- the interruption budget ------------------------------------------------

    def _quiet_now(self) -> bool:
        """True when local time is inside the quiet window — the user means
        *their* night, so this reads the clock naively."""
        return _in_quiet_window(self._quiet, datetime.now())

    def reparse_quiet_hours(self) -> None:
        """A chat-made change to agent.quiet_hours, applied now — the
        window is parsed once at boot, so the parse has to be redone on
        change (a malformed value refuses at the config layer, never
        here)."""
        self._quiet = _parse_quiet_hours(self._config.agent.quiet_hours)

    async def _triage_unprompted(
        self,
        provider: Provider,
        observations: list[Event],
        candidate: str,
    ) -> TriageDecision:
        """One typed classification of free text on an unprompted turn.

        A failure keeps the note internal, so a broken triage can never put
        words in front of the person.
        """
        try:
            return await asyncio.wait_for(
                triage_unprompted(
                    provider,
                    await self._system_prompt(),
                    observations="\n".join(self._event_line(event) for event in observations),
                    candidate=candidate,
                ),
                timeout=min(30.0, self._config.agent.turn_timeout_seconds),
            )
        except Exception:
            log.warning("unprompted triage failed — keeping the note internal", exc_info=True)
            return TriageDecision(False, "triage failed")

    async def _notify(
        self, text: str, *, urgent: float = 0.0, channel: str = ""
    ) -> dict[str, str] | None:
        """Send an unprompted note under the interruption budget: during
        quiet hours it waits (batched, shipped when the window ends) unless
        it is urgent enough on the 0-10 salience scale. Solicited words —
        replies to the user, approvals, scheduled deliveries — never come
        through here. `channel`, when given, narrows the words to that one
        surface; empty means every surface. Returns where the words landed,
        or None when they are being held (or empty) — held words haven't
        reached a surface, so there is nothing to link yet."""
        if not is_deliverable(text):
            return None
        if self._quiet_now() and urgent < self._config.agent.quiet_urgent_salience:
            self._held.append(text)
            # the words never ride the log — held text is the message
            # itself, and it ships whole when the window ends
            log.info("held for quiet hours (%d chars)", len(text))
            return None
        only = {channel} if channel else None
        return await self._surfaces.send_to_user(text, only=only)

    async def note_to_user(
        self, text: str, *, urgent: float = 0.0, channel: str = ""
    ) -> dict[str, str] | None:
        """The unprompted-note seam for the rest of the app — the
        background worker's plain notifications ride the same
        interruption budget (quiet hours, holding, batching) as the
        loop's own words, so they can never nag around the user's night.
        `channel` narrows the words to one surface when a caller names it."""
        return await self._notify(text, urgent=urgent, channel=channel)

    def attach_proactive_path(self, path: Any) -> None:
        """Join the commitment-reminder path after boot builds it — the same
        late-wire shape as the enrichment pieces, because the path delivers
        through this loop's interruption budget."""
        self._proactive_path = path

    def attach_alerts(self, alerts: AlertEngine) -> None:
        """Join the alert engine: it delivers through the surfaces and runs a
        folded routine action through this loop's authorization gate."""
        self._alerts = alerts
        alerts.attach_notify(self._notify)
        alerts.attach_action_runner(self._run_alert_action)

    async def _run_alert_action(self, alert: Any, event: Event) -> None:
        """Run one alert's optional action through the ordinary gate.

        A folded routine is still a tool call: it is classified, parked and
        audited exactly as the model's own proposal, under a provenance that
        says an alert triggered it."""
        action = alert.spec.action or {}
        call = ToolCall(
            id=f"alert-{alert.id}-{event.id}",
            name=str(action.get("tool", "")),
            arguments=dict(action.get("params") or {}),
        )
        calls: list[dict[str, Any]] = []
        with use_origin(alert_action(alert.id, event.id)):
            try:
                result = await self._execute(call, trace=calls)
            except Exception as exc:
                log.exception("alert %s action failed", alert.id)
                result = ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    content=f"alert action failed: {exc}",
                    is_error=True,
                )
                self._note_call(calls, call, result)
        seq = await self._audit.append(
            actor="alert",
            tool_name=call.name,
            decision="info",
            params={"event_id": event.id, "event": f"{event.source}/{event.kind}"},
            outcome="alert action fired",
        )
        await self._save_trace(
            kind=ALERT,
            label=alert.title,
            payload={
                "alert": {"id": alert.id, "title": alert.title},
                "event": {
                    "id": event.id,
                    "source": event.source,
                    "kind": event.kind,
                    "line": self._event_line(event),
                },
                "calls": calls,
                "audit_seq": seq,
            },
        )

    async def present_result(
        self,
        task_id: int,
        text: str,
        options: Sequence[str] = (),
        *,
        attachment: Attachment | None = None,
    ) -> None:
        """A finished background task's result, with its one-tap verdicts
        attached where the surfaces can carry buttons. Rides the same
        interruption budget as every other unprompted word: during quiet
        hours the words are held and batched for the morning (no buttons —
        a verdict tap hours later would hit a world that moved on), and
        the task stays in the panel for its decision instead. Media
        rides only when the words go out — a held result's file is in
        the store and the panel shows the task."""
        if not is_deliverable(text):
            return
        if self._quiet_now():
            self._held.append(text)
            # same rule as _notify: the words stay out of the log
            log.info("held for quiet hours (%d chars)", len(text))
            return
        media = {"attachment": attachment} if attachment is not None else {}
        await self._surfaces.present_result(task_id, text, options, **media)

    # -- the loop ---------------------------------------------------------------

    async def run(self) -> None:
        self._last_event_id = await self._events.max_id()
        log.info(
            "agent loop started (tick=%ss, last event id %d)",
            self._config.agent.tick_seconds,
            self._last_event_id,
        )
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("agent tick failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._woken.wait(), timeout=self._next_wait())
            self._woken.clear()

    def _next_wait(self) -> float:
        """The loop's sleep: the tick cadence, pulled in when a pending
        retry comes due sooner. A retry never waits for the next tick by
        accident, so 'I'll try again in about 20 seconds' stays true."""
        wait = float(self._config.agent.tick_seconds)
        if self._resumes:
            soonest = min(resume.due_at for resume in self._resumes)
            remaining = (soonest - datetime.now(UTC)).total_seconds()
            wait = min(wait, max(0.05, remaining))
        return wait

    async def _run_proactive_tick(self) -> None:
        """Turn due commitment reminders into an unprompted turn.

        The path decides which lead is due and records it; it sends nothing.
        The agent sees the due facts as an observation and writes the words
        itself, under the same interruption budget (quiet hours) as any other
        unprompted turn. A broken path costs a log line, never the tick.
        """
        if self._proactive_path is None:
            return
        try:
            due = await self._proactive_path.tick()
        except Exception:
            log.warning("proactive path tick failed — continuing", exc_info=True)
            return
        if not due:
            return
        observations = [self._reminder_event(item) for item in due]
        try:
            await self._turn([], observations)
        except Exception:
            log.exception("reminder turn failed")

    async def _run_alert_tick(self) -> None:
        """Deliver alert candidates that are due: a reminder moment, a held
        digest, or a failed send whose retry time arrived. Code decides and
        code sends — no unprompted turn is required for a notification."""
        if self._alerts is None:
            return
        try:
            await self._alerts.tick()
        except Exception:
            log.exception("alert delivery pass failed — continuing")

    @staticmethod
    def _reminder_event(item: Any) -> Event:
        """One due reminder as an observation for the agent — facts only."""
        payload = dict(getattr(item, "payload", {}) or {})
        return Event(
            id=0,
            source="proactive",
            kind="commitment_due",
            occurred_at=datetime.now(UTC),
            payload=payload,
            salience_score=float(getattr(item, "salience", 0.0) or 0.0),
            memorable=False,
            meta={},
            urgency=int(getattr(item, "urgency", 0) or 0),
            summary=str(payload.get("text") or ""),
        )

    async def _run_due_resumes(self) -> None:
        """Try every pending turn whose wait is over, oldest first. Each
        one either lands and delivers its answer, or schedules the next
        attempt; nothing here raises into the tick."""
        if not self._resumes:
            return
        now = datetime.now(UTC)
        due = sorted(
            (resume for resume in self._resumes if resume.due_at <= now),
            key=lambda resume: resume.due_at,
        )
        for resume in due:
            if not any(item is resume for item in self._resumes):
                continue
            self._resumes = [item for item in self._resumes if item is not resume]
            try:
                await self._turn(resume.messages, resume.observations, resume=resume)
            except Exception:
                # _turn already said plain words for a provider failure; a
                # deeper bug is a log line, never a dead loop
                log.exception("resumed turn failed")

    async def _handle_provider_failure(
        self,
        exc: LLMProviderError,
        *,
        messages: list[InboundMessage],
        observations: list[Event],
        history: list[Message],
        calls: list[dict[str, Any]],
        turn_ref: str,
        attempt: int,
        trace: dict[str, Any],
        previous: ProviderFailure | None,
        records: list[Any] | None = None,
    ) -> bool:
        """Say a provider failure in plain words and, when it is the kind
        that clears on its own, hand the turn back to the loop to finish
        once the service answers again. Returns True when a retry was
        scheduled — the inbound stays open for it — else False."""
        failure = classify_provider_failure(exc)
        # the record keeps plain words, never the SDK's raw error — the
        # panel renders this line, and a person should be able to read it
        trace["error"] = failure.message()
        trace["failure"] = failure.kind
        log.warning(
            "turn %s failed on the provider (%s, attempt %d): %s",
            turn_ref,
            failure.kind,
            attempt,
            exc,
        )
        # an unprompted tick stays quiet: nobody asked, so a background
        # hiccup is a log line, never an interruption
        if not messages:
            return False
        if not failure.retryable:
            await self._surfaces.send_to_user(failure.message())
            return False
        if attempt >= MAX_TURN_ATTEMPTS:
            await self._surfaces.send_to_user(failure.exhausted)
            return False
        # exponential backoff, capped so a long outage keeps checking in
        delay = min(failure.retry_after * (2 ** (attempt - 1)), 120.0)
        if previous is None or previous.kind != failure.kind:
            # the first failure — or a new kind of failure — always says the
            # cause and when the next attempt comes. Repeats of the same
            # outage stay quiet, so a long wait is not a drip of notes.
            await self._surfaces.send_to_user(failure.message(delay))
        self._schedule_resume(
            messages=messages,
            observations=observations,
            history=history,
            calls=calls,
            turn_ref=turn_ref,
            failure=failure,
            attempt=attempt,
            delay=delay,
            records=records,
        )
        return True

    def _schedule_resume(
        self,
        *,
        messages: list[InboundMessage],
        observations: list[Event],
        history: list[Message],
        calls: list[dict[str, Any]],
        turn_ref: str,
        failure: ProviderFailure,
        attempt: int,
        delay: float,
        records: list[Any] | None = None,
    ) -> None:
        # a person can queue a few asks during an outage, not an unbounded
        # pile — the oldest is dropped when the cap is reached
        if len(self._resumes) >= MAX_PENDING_RESUMES:
            dropped = self._resumes.pop(0)
            log.info("dropped the oldest pending retry (turn %s)", dropped.turn_ref)
        self._resumes.append(
            _Resume(
                messages=list(messages),
                observations=list(observations),
                history=list(history),
                calls=list(calls),
                turn_ref=turn_ref,
                failure=failure,
                attempts=attempt,
                due_at=datetime.now(UTC) + timedelta(seconds=max(0.05, delay)),
                records=list(records or []),
            )
        )

    def _spawn_scoring(self, event_id: int) -> None:
        """Score one event on a background task — the salience judge is a
        whole LLM turn, and words a user is waiting on must never queue
        behind it. A failure is logged, never raised into the tick."""

        async def _score() -> None:
            try:
                await self._salience.score_event(event_id)
            except Exception:
                log.exception("background salience scoring failed for event %s", event_id)

        task = asyncio.create_task(_score(), name=f"salience-score-{event_id}")
        self._scoring_tasks.add(task)
        task.add_done_callback(self._scoring_tasks.discard)

    def _spawn_summary_tick(self) -> None:
        """Write one due summary on a background task — the summarizer is a
        whole LLM turn, and it must never sit in front of a reply. At most
        one pass is in flight, and a failure is a log line, not a dead tick."""
        if self._summary_job is None:
            return
        if self._summary_task is not None and not self._summary_task.done():
            return

        async def _summarize() -> None:
            try:
                await self._summary_job.tick()
            except Exception:
                log.warning("summary generation failed — continuing", exc_info=True)

        self._summary_task = asyncio.create_task(_summarize(), name="memory-summary")

    async def _tick(self) -> None:
        anchor = self._last_event_id

        # (a) due source polls first, so their results are observed this
        # tick — unless someone is waiting on a reply: a message never
        # queues behind the feeds. When one is queued the polls defer to
        # this tick's tail (past the turn, or the quiet return below);
        # their own notify() wakes the next tick to observe what they
        # found
        polled = False
        if self._queue.empty():
            # a turn an outage interrupted comes first when its wait is
            # over — the user was told it would resume, and a message that
            # arrived meanwhile still gets its own turn below
            await self._run_due_resumes()
            await self._run_due_polls()
            await self._run_due_provider_polls()
            polled = True

        # (a2) a server that answered late joins the namespace here —
        # keeping the "the moment it's up" promise is the tick's own job
        await self._reconcile_mcp()

        # (a3) pending approvals past their TTL flip to expired and are
        # audited — without this they'd sit as 'pending' forever, and every
        # pending-approvals reader would keep offering dead decisions
        if self._approvals is not None:
            try:
                await self._approvals.expire_overdue()
            except Exception:
                log.exception("expiring overdue approvals failed — continuing")

        # (b) inbound chat becomes memory (allowlist applies at ingest; a
        # dropped sender is never seen by the model at all)
        drained: list[InboundMessage] = []
        while not self._queue.empty():
            drained.append(self._queue.get_nowait())
        messages: list[InboundMessage] = []
        records: list[Any] = []
        for message in drained:
            log.info(
                "drained inbound: surface=%s chat=%s message=%s text_chars=%d attachments=%d",
                message.surface,
                message.chat_ref,
                message.message_id or "?",
                len(message.text),
                len(message.attachments),
            )
            # the deterministic vocabulary answers here, before anything is
            # ingested or any model is woken: a /verify badge and a why?
            # replay cost no tokens and store no memory — they are reads,
            # answered from the record
            if await self._try_chat_command(message):
                continue
            if await self._try_explain_reply(message):
                continue
            result = await self._events.ingest(
                source=message.surface,
                kind="chat_message",
                payload={"text": message.text, "chat_ref": message.chat_ref},
                sender=Sender(platform=message.surface, handle=message.handle),
            )
            if result.stored and result.event_id is not None:
                # the judge leaves the reply path here — scored beside the
                # turn instead of in front of it
                self._spawn_scoring(result.event_id)
                self._last_event_id = max(self._last_event_id, result.event_id)
            if result.reason == "filtered:contacts":
                log.info("inbound on %s filtered by the allowlist", message.surface)
                continue
            # (b1) the durable inbound ledger: an update re-delivered with
            # the same platform message id is one row, so it can only ever
            # produce one model turn. A duplicate stops here.
            try:
                record = await self._inbound_states.record(
                    message.surface, message.chat_ref, message.message_id
                )
            except Exception:
                # a ledger failure is never a reason to lose a real message:
                # process it, just without a recorded outcome
                log.warning("inbound ledger failed — processing without it", exc_info=True)
                record = None
                duplicate = False
            else:
                duplicate = record is None
            if duplicate:
                log.info(
                    "duplicate inbound skipped: surface=%s message=%s",
                    message.surface,
                    message.message_id or "?",
                )
                continue
            records.append(record)
            # (b2) files the user shared: described (images) or extracted
            # (documents) into memory here — past the allowlist, before the
            # observation list is built, so they reach this same tick's turn
            if self._attachments is not None and message.attachments:
                try:
                    results = await self._attachments.process(message)
                except Exception:
                    log.exception("attachment processing failed — continuing")
                    results = []
                for one in results:
                    if (
                        one.ingested is not None
                        and one.ingested.stored
                        and one.ingested.event_id is not None
                    ):
                        self._last_event_id = max(self._last_event_id, one.ingested.event_id)
                note = attachments_note(results)
                if note:
                    # solicited — the user just shared something, so it never
                    # waits out quiet hours
                    await self._surfaces.send_to_user(note)
            # transcript write happens only past the allowlist — a filtered
            # sender must never reach conversation memory, or their words
            # would leak into a later prompt. Web inbound is persisted by
            # the websocket handler itself, so it's skipped here.
            if self._transcript is not None and message.surface != "web":
                try:
                    await self._transcript.append(message.surface, "in", message.text)
                except Exception:
                    log.exception("transcript write failed for inbound %s", message.surface)
            messages.append(message)

        # (b3) a failed reply is a code concern: the fanout already sealed it
        # in the delivery record, `_redeliver_failed` retries it here with no
        # model call, and the record also becomes an event for the panel.
        # That event is stripped from the model's observations below — a
        # delivery hiccup must never trigger a turn or read as a new request.
        await self._record_delivery_notices()
        self._delivery_lines = await self._redeliver_failed()
        await self._refresh_owner_addresses()
        # a received message older than the limit is never auto-answered
        await self._expire_stale_inbounds()

        # (c) everything else ingested since last tick becomes observations —
        # except delivery/diagnostic events, which the model never sees
        observations = await self._events.list_since(anchor, limit=_MAX_OBSERVATIONS)
        for event in observations:
            self._last_event_id = max(self._last_event_id, event.id)
        observations = [event for event in observations if event.kind != "delivery_failed"]
        if messages:
            # a reply is waiting — the scoring spawns beside the turn; the
            # verdicts land moments later and the next reader (recall,
            # later turns) sees them
            for event in observations:
                self._spawn_scoring(event.id)
        else:
            # nobody spoke, so the turn ahead is unprompted and its
            # interruption budget reads these very scores — they stay
            # inline, fresh when urgent is computed
            for event in observations:
                await self._salience.score_event(event.id)

        # (c2) standing triggers the user taught — deterministic matching
        # over the same observations, before any LLM is involved

        # held unprompted notes from the quiet window ship as one batched
        # message the moment the window ends — the budget pays out, in one
        # interruption instead of a drip
        if self._held and not self._quiet_now():
            held, self._held = self._held, []
            await self._surfaces.send_to_user("While it was quiet:\n\n" + "\n\n".join(held))

        if not messages and not observations:
            # a quiet tick: no LLM turn, no tokens spent — but the polls a
            # queued message deferred still run before the tick closes
            if not polled:
                await self._run_due_polls()
                await self._run_due_provider_polls()
            # a retry that came due while the queue was busy still runs
            await self._run_due_resumes()
            await self._run_proactive_tick()
            await self._run_alert_tick()
            self._spawn_summary_tick()
            return

        # more than one question pending is answered question-by-question, so
        # each reply can be bound to its own message; a single message is the
        # ordinary one-turn case
        pending_now = 0
        try:
            pending_now = len(await self._inbound_states.pending(limit=5))
        except Exception:
            pending_now = 0
        if len(messages) > 1:
            for index, (message, record) in enumerate(zip(messages, records, strict=True)):
                await self._turn(
                    [message],
                    observations if index == 0 else [],
                    records=[record],
                    force_quote=pending_now > 1,
                )
        else:
            await self._turn(messages, observations, records=records, force_quote=pending_now > 1)

        # the polls the waiting message deferred — after the reply, never
        # before it
        if not polled:
            await self._run_due_polls()
            await self._run_due_provider_polls()

        # a retry that came due during this turn, or one the turn itself
        # just failed, runs now rather than waiting out the next tick
        await self._run_due_resumes()
        # reminders and alerts held by a quiet window — never in front of
        # the words someone is waiting on
        await self._run_proactive_tick()
        await self._run_alert_tick()
        self._spawn_summary_tick()

        # a tick that never suspended (a provider-less turn, say) still
        # lets the scorers it spawned take their first step here, so none
        # is left pending at loop close
        await asyncio.sleep(0)

    # -- the deterministic chat vocabulary ---------------------------------------

    async def _try_chat_command(self, message: InboundMessage) -> bool:
        """Answer the deterministic vocabulary straight from the record or
        the live state — no LLM turn, no tokens, nothing ingested, no
        trace. A /config walk in progress answers here too: its questions
        and replies are part of the same vocabulary. Returns True when the
        message was a handled command."""
        text = message.text.strip()
        first = text.split(maxsplit=1)[0].lower() if text else ""
        if first == "/config":
            # bare /config is the friendly front door: the guided walk. A
            # mid-walk /config starts it over, and an expert line replaces
            # any walk in progress — the user spoke expert, honor it.
            if len(text.split()) == 1 and self._config_manager is not None:
                self._apps_walk = None  # one walk at a time
                self._wizard = ConfigWizard(
                    self._config,
                    apply=self._apply_config,
                    show=self._config_manager.show,
                    yaml_value=self._config_manager.yaml_value,
                )
                await self._surfaces.send_to_user(self._wizard.start())
                return True
            self._wizard = None
            return await self._config_command(text)
        if self._wizard is not None:
            if text.startswith("/"):
                self._wizard = None  # a command ends the walk quietly
            else:
                wizard = self._wizard
                reply = await wizard.handle(text)
                if reply is None:
                    # the walk let go of the message — it's normal chat
                    self._wizard = None
                    return False
                if not wizard.alive:
                    self._wizard = None  # goodbye — no menu is waiting
                await self._surfaces.send_to_user(reply)
                return True
        if first == "/apps":
            # the app front door: bare /apps is the status, /apps add <name>
            # starts the connect walk. Either replaces a live config walk,
            # as a bare /config replaces an apps one.
            self._wizard = None
            self._apps_walk = self._new_apps_wizard()
            parts = text.split(maxsplit=2)
            if len(parts) >= 2 and parts[1].lower() == "add":
                name = parts[2] if len(parts) > 2 else ""
                reply = await self._apps_walk.start_add(name)
            else:
                reply = await self._apps_walk.start()
            await self._surfaces.send_to_user(reply)
            if not self._apps_walk.alive:
                self._apps_walk = None
            return True
        if self._apps_walk is not None:
            if text.startswith("/"):
                self._apps_walk = None  # a command ends the walk quietly
            else:
                walk = self._apps_walk
                reply = await walk.handle(text)
                if reply is None:
                    # the walk let go of the message — it's normal chat
                    self._apps_walk = None
                    return False
                if not walk.alive:
                    self._apps_walk = None  # done, or goodbye — nothing is waiting
                await self._surfaces.send_to_user(reply)
                return True
        if self._profile_confirm is not None:
            # one shot: the batched confirmation listed these entries, and
            # whatever comes next either confirms them or is a correction
            armed, self._profile_confirm = self._profile_confirm, None
            if not text.startswith("/"):
                if text.strip().lower() in ("yes", "y", "ok", "okay", "confirm", "all good"):
                    confirmed = await self._workspace.confirm_profile(armed)
                    reply = (
                        f"kept {confirmed} of them — thank you."
                        if confirmed
                        else "nothing was still waiting — already settled."
                    )
                    await self._surfaces.send_to_user(reply)
                    return True
                # a correction — normal chat: the model hears it and can
                # supersede the wrong entries through the workspace tools
                return False
            # a command while the confirmation waited: the wait is over and
            # the command runs below — the entries stay pending for later
        if first == "/profile":
            self._wizard = None
            self._apps_walk = None
            await self._surfaces.send_to_user(await self._profile_command(text))
            return True
        if first in ("/pause", "/resume"):
            self._wizard = None
            self._apps_walk = None
            await self._surfaces.send_to_user(await self._pause_resume_command(first))
            return True
        if text.lower() == "/status":
            self._wizard = None
            self._apps_walk = None
            await self._surfaces.send_to_user(await self._status_command())
            return True
        if text.lower() != "/verify":
            return False
        try:
            verification = await self._audit.verify_chain()
            newest = None
            if verification.entries:
                rows = await self._audit.recent(1)
                newest = rows[0]["created_at"] if rows else None
            # the render sits inside the same guard: a bug in the badge
            # wording must degrade to the fallback note, not kill the tick
            # and take every other batched message down with it
            text = verification_text(verification, newest)
        except Exception:
            log.exception("/verify could not read the decision record")
            await self._surfaces.send_to_user(
                "I couldn't verify the record just now — the decision "
                "log isn't answering. Nothing else is affected."
            )
            return True
        # solicited — the user asked for the check, so it never waits out
        # quiet hours
        await self._surfaces.send_to_user(text)
        return True

    async def _status_command(self) -> str:
        """The plain state of the senses and the queues — resolved models,
        not raw config knobs, so a "what can you actually see?" question is
        answered from the truth the running process serves with."""
        lines: list[str] = []

        def resolved(role: str, doing: str) -> str:
            provider = self._providers.for_role(role) if self._providers else None
            if provider is None:
                return f"{doing}: nothing set"
            return f"{doing}: {provider.model}"

        lines.append(resolved("reasoning", "main model"))
        lines.append(resolved("image", "reading images"))
        lines.append(resolved("audio", "listening to audio"))
        lines.append(resolved("video", "watching video"))
        if self._config_manager is not None:
            try:
                effective = await self._config_manager.effective()
                overrides = effective.get("overrides") or []
                if overrides:
                    lines.append(
                        "changed from config.yaml: "
                        + ", ".join(str(entry.get("path")) for entry in overrides)
                    )
                else:
                    lines.append("nothing changed from config.yaml")
            except Exception:
                log.exception("/status could not read the settings ledger")
        if self._approvals is not None:
            pending_rows = await self._approvals.list_pending()
            lines.append(
                f"{len(pending_rows)} approval(s) waiting for you"
                if pending_rows
                else "no approvals waiting"
            )
        depth = self._queue.qsize()
        lines.append(f"{depth} message(s) queued" if depth else "nothing queued")
        if restart_pending():
            lines.append("a restart is coming to load saved settings — back in a few seconds")
        return "Right now:\n" + "\n".join(f"- {line}" for line in lines)

    async def _config_command(self, text: str) -> bool:
        """The /config command, deterministic end to end: no ingest, no
        model, no tokens. `show` is a read answered from the live config;
        every write goes through the executor as a synthetic set_config
        call — the same choke point as the model's proposals, so a
        security path parks for one-tap approval exactly like any other
        gated action."""
        try:
            reply = await self._run_config_command(text)
        except Exception:
            log.exception("/config failed")
            reply = "that didn't work — the configuration is untouched. Nothing else is affected."
        # solicited — the user asked, so it never waits out quiet hours
        await self._surfaces.send_to_user(reply)
        return True

    async def _run_config_command(self, text: str) -> str:
        if self._config_manager is None:
            return "config management isn't wired on this instance."
        op, path, value = _parse_config_command(text)
        if op == "usage":
            return _CONFIG_USAGE
        if op == "show":
            return await self._config_manager.show(path)
        if value is _MISSING and op in ("set", "add", "remove"):
            if op == "set":
                return (
                    "set needs a value — e.g. /config set agent.tick_seconds 10 "
                    "(or the word none to clear a setting)."
                )
            return (
                f"{op} needs a value after the path — e.g. "
                f"/config {op} contacts.allowlist telegram @friend."
            )
        return await self._apply_config(op, path, value)

    async def _apply_config(
        self, op: str, path: str, value: Any = _MISSING, origin: str | None = None
    ) -> str:
        """One config change through the executor — the same synthetic
        set_config call for both /config front doors (the guided walk and
        the one-line grammar), so a write is classified, audited, parked or
        applied exactly like a model proposal. `value` absent means reset,
        which carries none. `origin` marks a connect the user just drove in
        chat ("walk") — the /apps pick-and-paste applies directly, where
        /config lines and model proposals still park."""
        params: dict[str, Any] = {"op": op, "path": path}
        if value is not _MISSING:
            params["value"] = value
        calls: list[dict[str, Any]] = []
        result = await self._execute(
            ToolCall(id="cmd-config", name="set_config", arguments=params),
            trace=calls,
            origin=origin,
        )
        held = calls[0].get("approval_id") if calls else None
        if held is not None:
            return (
                f"that one's security-shaped — held for your one-tap "
                f"approval (#{held}). Tap approve and it's done."
            )
        return result.content

    # -- /profile: who the user is, seeded and confirmed deterministically ----

    async def _profile_command(self, text: str) -> str:
        """/profile — the deterministic front door to the user's profile.
        `/profile <one line>` seeds a confirmed entry straight from their
        own words; bare /profile shows what's confirmed and gathers
        everything the agent inferred into one batched confirmation. No
        model, no ingest, no tokens — the same vocabulary as /verify."""
        if self._workspace is None:
            return "the workspace isn't on this instance, so there's no profile to keep."
        parts = text.split(maxsplit=1)
        line = parts[1].strip() if len(parts) > 1 else ""
        if line:
            try:
                await self._workspace.remember(line, "profile", source="user")
            except SecretRejected:
                return "that looks like a secret or key — I won't store that."
            except ValueError:
                return "that didn't look like a line I could keep."
            except WorkspaceWriteError as exc:
                return f"not saved — {exc}"
            return (
                "Noted — part of your profile now. Say /profile anytime to see or fix what I have."
            )
        confirmed = await self._workspace.profile()
        pending = await self._workspace.pending_profile()
        if pending:
            self._profile_confirm = [e.id for e in pending]
            listed = "\n".join(f"{i}. {e.text}" for i, e in enumerate(pending, start=1))
            return (
                "Here is what I have gathered about you:\n"
                f"{listed}\n"
                "Reply yes to keep all of it, or tell me what to fix."
            )
        if confirmed:
            shown = "\n".join(f"- {e.text}" for e in confirmed)
            return f"Your profile so far:\n{shown}\nNothing new is waiting."
        return "I don't have anything about you yet — /profile <one line> tells me who you are."

    # -- /pause /resume: the background worker's switch ---------------------------

    async def _pause_resume_command(self, word: str) -> str:
        """/pause and /resume — the background worker's switch in plain
        words. The user's own word is the approval, so the write rides the
        same executor path as a /config line — classified, audited,
        persisted — and the worker reads the switch live on its next
        cycle. No model, no ingest, no tokens."""
        want = word == "/resume"
        if self._config_manager is None:
            return "config management isn't wired on this instance, so there's no background worker to switch."
        if self._config.proactive.enabled == want:
            return (
                "the background worker is already running — say /pause to stop it."
                if want
                else "the background worker is already off — say /resume when you want it back."
            )
        return await self._apply_config("set", "proactive.enabled", want)

    # -- /apps: the status render and the connect plumbing ---------------------------

    async def _env_names(self) -> set[str]:
        """Which secret names exist right now — names only, never values:
        pastes, .env, the real environment. An unwired resolver (a bare
        loop, tests) reads as "nothing set yet", the honest answer for a
        loop that can't check."""
        if self._resolver is None:
            return set()
        return await self._resolver.known_names()

    async def _store_secret(self, name: str, value: str, app: str) -> bool:
        """One pasted key into the encrypted store. Called from the
        deterministic command path — before ingest — so the value is
        consumed here and never reaches the model, memory, or the audit
        record, which carries the name and the app only. False when this
        loop has no store wired, and the walk answers honestly below."""
        if self._secret_store is None:
            return False
        await self._secret_store.set(name, value)
        await self._audit.append(
            actor="owner",
            tool_name="store_app_secret",
            decision="allow",
            rules_matched="builtin:internal",
            params={"name": name, "app": app},
            outcome="stored encrypted — value never recorded",
        )
        return True

    async def _apps_status(self) -> str:
        """What's connected, honestly — the same render at bare /apps and
        after a connect. A messaging surface that's on without its token
        says so; a direct provider app names its identity (or its
        non-active status); an mcp server counts its actions when it has
        answered and makes the promise when it hasn't. The footer is the
        gate, in one line — connection status and what Aether may do are
        separate truths."""
        names = await self._env_names()
        lines: list[str] = []
        messaging = self._config.messaging
        for platform in ("telegram", "discord"):
            if not getattr(messaging, platform).enabled:
                continue
            if all(name in names for name in _PLATFORM_TOKENS[platform]):
                lines.append(f"· {platform} — on")
            else:
                lines.append(f"· {platform} — on — no token yet")
        if self._provider_runtime is not None:
            try:
                feed = await self._provider_runtime.feed()
            except Exception:
                log.exception("reading direct provider connections failed")
                feed = {"apps": []}
            for app in feed.get("apps", []):
                key = str(app.get("key", ""))
                name = str(app.get("name", key))
                status = str(app.get("status", "not_connected"))
                who = str(app.get("identity", ""))
                if status == "active":
                    suffix = f" — {who}" if who else ""
                    lines.append(f"· {name}{suffix} — connected")
                elif status == "reauth_required":
                    lines.append(f"· {name} — needs reconnecting — /apps add {key} to reconnect")
                elif app.get("setup_required"):
                    lines.append(f"· {name} — not connected — finish the {name} setup first")
                else:
                    lines.append(f"· {name} — not connected — /apps add {key} to connect")
        status = self._tools.mcp_status()
        for server in self._config.mcp_servers:
            if not server.enabled or is_legacy_composio_server(server.name):
                continue
            if status.get(server.name):
                count = self._tools.server_action_count(server.name)
                lines.append(f"· {server.name} — connected · {_actions_word(count)}")
            else:
                lines.append(
                    f"· {server.name} — still connecting — I'll tell you the moment it's up"
                )
        head = "your apps:\n" + "\n".join(lines) if lines else "your apps: nothing connected yet."
        return f"{head}\nthe gate: reads run free — sends, deletes and anything new ask first."

    def _new_apps_wizard(self) -> AppsWizard:
        """Every walk wires the same loop functions — the connect command
        path and the callback's permissions walk alike."""
        return AppsWizard(
            apply=self._apply_config,
            status=self._apps_status,
            env_names=self._env_names,
            save_secret=self._store_secret,
            provider_apps=self._provider_apps,
            connect_provider=self._connect_provider_reply,
        )

    async def _provider_apps(self) -> list[tuple[str, str]]:
        if self._provider_runtime is None:
            return []
        try:
            feed = await self._provider_runtime.feed()
        except Exception:
            log.exception("reading direct provider apps failed")
            return []
        return [
            (str(app.get("key", "")), str(app.get("name", "")))
            for app in feed.get("apps", [])
            if app.get("key")
        ]

    async def _connect_provider_reply(self, app_key: str) -> ConnectResult:
        """Start one direct-provider sign-in from chat. The wizard owns the
        setup recipe and the paste asks; this only returns the provider's
        answer or raises a plain ProviderError."""
        runtime = self._provider_runtime
        if runtime is None:
            raise ProviderNotConfigured(
                "direct app connections aren't wired on this instance — "
                "set AETHER_TOKEN_ENCRYPTION_KEY and AETHER_TOKEN_ENCRYPTION_KEY_ID, "
                "then restart."
            )
        return await runtime.connect(app_key)

    async def announce_provider_connection(self, app_key: str, identity: str = "") -> None:
        """The OAuth callback landed — move the walk to the gate's question:
        what may Aether do with this app. The answer writes an authz rule
        with the direct provider prefix.

        The account address the provider just reported is the owner's own,
        so it is claimed here: without it the first mail from a
        second address of the user's would render as a stranger's.
        """
        if self._entities is not None and identity.strip():
            try:
                await self._entities.mark_owner_address(app_key, identity.strip())
            except Exception:
                log.warning("%s connected but its own address could not be claimed", app_key)
        walk = self._apps_walk
        if walk is None or not walk.alive:
            walk = self._new_apps_wizard()
            self._apps_walk = walk
        await self._surfaces.send_to_user(
            walk.enter_permissions(app_key, identity, prefix=f"{app_key}__")
        )

    async def _try_explain_reply(self, message: InboundMessage) -> bool:
        """A reply that is just 'why?' (or 'explain') about one of my own
        messages: the recorded trace of that message answers,
        deterministically — no LLM turn. Anything else — other words, or a
        message nobody recorded — falls through to the normal path."""
        if not message.reply_to_id:
            return False
        if message.text.strip().rstrip("?!").lower() not in WHY_REPLY_WORDS:
            return False
        trace = await self._trace_for_ref(message.surface, message.reply_to_id)
        if trace is None:
            return False  # nothing links that message — the model answers
        # solicited by the reply, so the replay is sent directly
        await self._surfaces.send_to_user(plain_replay(trace))
        return True

    async def _trace_for_ref(self, surface: str, ref: str) -> Any | None:
        """The newest trace whose recorded delivery includes this platform
        message id on this surface — the why? lookup. Scans recent traces;
        a ref older than that window falls through to the model, which can
        still answer with explain_decision."""
        if self._traces is None:
            return None
        for trace in await self._traces.recent(50):
            if (trace.payload or {}).get("chat_refs", {}).get(surface) == ref:
                return trace
        return None

    # -- source polling -----------------------------------------------------------

    def rebuild_poll_targets(self) -> None:
        """Rebuild the poll schedule from the host's current servers — the
        boot block, factored out so a chat-made mcp_servers change can
        re-run it against the new live set. Servers that left take their
        stale watchdog state with them."""
        self._poll_targets.clear()
        self._next_poll.clear()
        if self._host is None:
            return
        now = datetime.now(UTC)
        for conn in self._host.connections:
            for i, poll in enumerate(conn.poll_tools):
                key = f"{conn.name}:{poll.tool}:{i}"
                self._poll_targets[key] = (conn.name, poll)
                self._next_poll[key] = now  # the first tick runs every poll once
        live = {conn.name for conn in self._host.connections}
        self._poll_failures = {k: v for k, v in self._poll_failures.items() if k in live}
        self._down = {s for s in self._down if s in live}

    async def _run_due_polls(self) -> None:
        """Run every configured poll that is due. The polls double as the
        connector watchdog's heartbeat: consecutive failures mark a server
        down (announced once), and the next good poll announces the
        recovery. Intervals carry a little jitter, and a failing feed backs
        off instead of retrying at full speed every tick."""
        if not self._poll_targets:
            return
        now = datetime.now(UTC)
        jitter = self._config.proactive.poll_jitter
        for key, (server, poll) in list(self._poll_targets.items()):
            if now < self._next_poll.get(key, now):
                continue
            qualified = f"{server}__{poll.tool}"
            try:
                result = await self._host.call(qualified, poll.args)  # type: ignore[union-attr]
            except (UnknownToolError, ConnectorUnavailableError) as exc:
                log.warning("source poll %s failed: %s", qualified, exc)
                failures = self._poll_failures.get(server, 0) + 1
                self._poll_failures[server] = failures
                self._next_poll[key] = now + timedelta(
                    minutes=next_poll_delay(poll.every_minutes, failures=failures, jitter=jitter)
                )
                if failures >= POLL_FAILURES_BEFORE_DOWN and server not in self._down:
                    self._down.add(server)
                    await self._notify(
                        f"{server} hasn't been responding — I've paused "
                        "relying on it and will say when it's back."
                    )
                continue
            self._next_poll[key] = now + timedelta(
                minutes=next_poll_delay(poll.every_minutes, failures=0, jitter=jitter)
            )
            # a good poll is the heartbeat: the failure count resets, and a
            # server that was announced down gets its recovery announced
            self._poll_failures.pop(server, None)
            if server in self._down:
                self._down.discard(server)
                await self._notify(f"{server} is responding again.")
            # the poll path calls the host directly — the registry's mail
            # filter never sees it — so the same allowlist rides here
            if self._mail_filter is not None:
                result = await self._mail_filter.filter_result(qualified, result)
            await self._audit.append(
                actor="scheduler",
                tool_name=qualified,
                decision="allow",
                rules_matched="config:poll_tools",
                params=poll.args,
                outcome="source poll",
            )
            ingest = await self._events.ingest(
                source=server,
                kind=f"poll:{poll.tool}",
                payload={"args": poll.args, "result": result[:4000]},
            )
            if ingest.stored:
                self.notify()

    async def _run_due_provider_polls(self) -> None:
        """Run due direct-provider polls (Gmail history, later others).

        The provider runtime owns the cursor and the schedule; this path
        owns the same audit row and event ingest the MCP polls use. Gmail
        results still pass the allowlist before the model can see them, and
        the allowlist judges the whole envelope — a sender it drops never
        reaches the split below, so nothing filtered can be stored.

        A mail poll is one event per message rather than one blob: the old
        single row was truncated to 4000 characters and merged every
        message of the poll into it, so nothing downstream could attribute,
        sort or filter a single mail.
        """
        if self._provider_runtime is None:
            return
        try:
            results = await self._provider_runtime.poll_due()
        except Exception:
            log.exception("provider polling failed — continuing")
            return
        for result in results:
            text = json.dumps(result.payload, ensure_ascii=False, default=str)
            if self._mail_filter is not None:
                text = await self._mail_filter.filter_result(result.tool, text)
            await self._audit.append(
                actor="scheduler",
                tool_name=result.tool,
                decision="allow",
                rules_matched="provider:poll",
                params={},
                outcome="provider poll",
            )
            if await self._ingest_mail(text, result):
                continue
            kind = result.tool.split("__", 1)[-1]
            ingest = await self._events.ingest(
                source=result.app,
                kind=f"poll:{kind}",
                payload={"result": text[:4000]},
            )
            if ingest.stored:
                self.notify()

    async def _ingest_mail(self, text: str, result: Any) -> bool:
        """One event per mail in a filtered poll envelope.

        The addresses stay in the payload exactly as the provider returned
        them; no sender rides the ingest, because the mail allowlist — not
        the contact allowlist — is what judges mail, and it has already run
        on this envelope. Re-judging here would drop mail the user *sent*
        to an allowlisted person, whose sender is the user themselves.

        Returns False when the envelope carries no messages, so the caller
        keeps its own shape for results this path does not model.
        """
        try:
            envelope = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            envelope = result.payload
        messages = split_mail(envelope)
        if not messages:
            return False
        stored = 0
        for message in messages:
            ingest = await self._events.ingest(
                source=MAIL_SOURCE,
                kind=MAIL_KIND,
                payload=message.payload,
                occurred_at=message.occurred_at,
                meta={
                    "provider": MAIL_SOURCE,
                    "message_id": message.message_id,
                    "thread_id": message.thread_id,
                    "from_address": message.sender,
                },
                dedup_key=message.dedup_key,
            )
            if ingest.stored:
                stored += 1
        if stored:
            log.info("ingested %d mail message(s) from %s", stored, result.app)
            self.notify()
        return True

    # -- the late-ready reconcile ---------------------------------------------------

    async def _reconcile_mcp(self) -> None:
        """Keep the tool namespace honest about servers that answered
        late: the sync runs at boot and at apply time, so a server that
        comes ready after its moment would otherwise never enter the
        namespace at all — its actions stay uncallable while the config
        says it's linked. Every tick, a server that's ready at the host
        but missing from the namespace is synced in, and its coming-up is
        said out loud — once per boot, so a flapping server never
        announces twice. A ready server whose tool list changed since the
        last sync is re-synced and its new actions announced the same way."""
        if self._host is None:
            return
        versions: dict[str, int] = {}
        late: set[str] = set()
        changed: set[str] = set()
        for conn in self._host.connections:
            if not conn.ready:
                continue
            versions[conn.name] = getattr(conn, "tool_version", 0)
            if not self._tools.has_mcp_server(conn.name):
                late.add(conn.name)
            elif conn.name in self._synced_tool_versions:
                if self._synced_tool_versions[conn.name] != versions[conn.name]:
                    changed.add(conn.name)
            else:
                # ready when its sync ran at boot or apply time — never
                # late, never announced from here; just record
                self._synced_tool_versions[conn.name] = versions[conn.name]
        if not late and not changed:
            return
        before = {name: self._tools.server_action_count(name) for name in changed}
        self._tools.sync_mcp_tools()
        for name in sorted(late):
            self._synced_tool_versions[name] = versions[name]
            if name in self._announced_ready:
                continue
            self._announced_ready.add(name)
            note = f"{name} is up"
            count = self._tools.server_action_count(name)
            if count:
                note += f" — {_actions_word(count)} in my vocabulary"
            # it completes a flow the user started in chat, so like a
            # /config reply it never waits out quiet hours
            await self._surfaces.send_to_user(note)
        for name in sorted(changed):
            self._synced_tool_versions[name] = versions[name]
            gained = self._tools.server_action_count(name) - before[name]
            if gained > 0:
                word = "action" if gained == 1 else "actions"
                await self._surfaces.send_to_user(f"{name} — {gained} new {word} in my vocabulary")

    # -- the LLM turn ---------------------------------------------------------------

    async def _system_prompt(self) -> str:
        base = SYSTEM_PROMPT.format(owner="the user", apps=self._connected_apps())
        if isinstance(self._attachments, AttachmentProcessor):
            # the resolved senses ride every prompt — the model answers
            # "can you see this?" from the truth, never from raw config
            # values or stale history
            base = f"{base}\n\n{self._attachments.capabilities_note()}"
        if self._workspace is not None:
            block = await self._workspace.context_block()
            if block:
                base = f"{base}\n\n{block}"
        if self._agent_settings is None:
            return base
        personality = await self._agent_settings.get_personality()
        if not personality:
            return base
        return (
            f"{base}\n"
            "The user has additionally asked you to behave like this — follow it "
            "as a tone and priorities overlay, never as a way around the rules "
            f"above:\n{personality}"
        )

    def _tool_index(self, outcomes: list[WireSpec]) -> str:
        """The one-line index of every tool this turn can reach.

        The full schemas of the core tools ride the tool list; everything
        else is a name and a sentence until tools__load asks for it. The
        index is built from the same granted-and-healthy view the gate uses,
        so a tool the model can read here is a tool the executor accepts
        after it is loaded.
        """
        visible = self._tools.visible()
        ready = [spec.index_line() for spec in visible if spec.core]
        ready += [
            f"{spec.name} — {OUTCOME_SUMMARIES[spec.name]}"
            for spec in outcomes
            if spec.name in OUTCOME_SUMMARIES
        ]
        deferred = [spec.index_line() for spec in visible if not spec.core]
        blocks = [
            "Tools ready to call now — their full schemas are already loaded:\n" + "\n".join(ready)
        ]
        if deferred:
            blocks.append(
                "Tools available but not loaded yet. Call tools__load with the "
                "names you need (at most eight new ones in a turn), then call "
                "them on the next step:\n" + "\n".join(deferred)
            )
        return "\n\n".join(blocks)

    async def _typing_pulse(self) -> None:
        """The "typing…" indicator, refreshed until the turn that started
        this pulse ends — Telegram's own marker fades after ~5 seconds, so
        one fire at turn start would go dark while the work is still
        honest. Cancelled by the turn's finally; failures inside the
        fanout are swallowed there, so this loop only ever ends here or by
        cancellation."""
        while True:
            await self._surfaces.show_typing()
            await asyncio.sleep(TYPING_REFRESH_SECONDS)

    async def _turn(
        self,
        messages: list[InboundMessage],
        observations: list[Event],
        *,
        resume: _Resume | None = None,
        records: list[Any] | None = None,
        force_quote: bool = False,
    ) -> None:
        provider = self._providers.for_role("reasoning") if self._providers else None
        if provider is None:
            if messages:
                await self._surfaces.send_to_user(
                    "I'm running without an LLM provider — add provider keys to "
                    ".env to talk to me. Feeds and schedules keep working."
                )
            return

        # the inbound rows this turn owes a terminal outcome
        if resume is not None:
            records = list(resume.records or [])
            force_quote = True  # a resumed answer is late by definition
        records = list(records or [])

        # the terminal tools are always offered; whether a send is a reply
        # or a proactive note is the turn type's decision, and quiet hours
        # hold a low-urgency note rather than blocking the offer
        outcome_tools = outcome_specs(solicited=bool(messages))
        offered_outcomes = {spec.name for spec in outcome_tools}

        recalled: list[dict[str, Any]] = []
        pipeline_result: Any = None
        if resume is None:
            # the recall query depends only on the message texts, so its
            # embedding starts now and the round trip overlaps the context
            # assembly below — one API wait hidden instead of added
            recall_embed: asyncio.Task[list[float] | None] | None = None
            await self._refresh_owner_addresses()
            # a solicited turn already holds the user's own words, so the
            # classifier running behind ingest is detail it does not need;
            # waiting for it here would put a model round trip in front of
            # every reply. An unprompted turn reads events, so it waits only
            # for those events, briefly, and never cancels their work.
            if not messages:
                await self._await_enrichment(observations)
            # code-only re-delivery, already done by the tick that built this
            # turn — never a model observation
            delivery_lines = list(self._delivery_lines)
            if messages and self._vectors is not None and self._vectors.enabled:
                try:
                    recall_query = " ".join(
                        [m.text for m in messages]
                        + [m.reply_to_text for m in messages if m.reply_to_text]
                    ).strip()
                    if recall_query:
                        recall_embed = asyncio.create_task(self._vectors.embed_query(recall_query))
                except Exception:
                    # a layer this broken recalls inline or not at all — either
                    # way the turn itself must survive
                    log.exception("recall head start failed — embedding inline instead")

            # the alias confirmation is a question, so it is only
            # loaded when the user is actually here — an unprompted tick must
            # never raise one
            ctx = await self._context.build(with_questions=bool(messages))
            history: list[Message] = []
            # every meaningful question pulls its own related memories in —
            # older conversations, shared documents, whatever the question
            # touches — without the model having to think to ask. The routed
            # pipeline (memory.query_pipeline) replaces the vector-only recall
            # and owns the attention block when it is on; otherwise the old
            # path answers exactly as before.
            recall_lines: list[str] = []
            pipeline_result = None
            pipeline_on = self._query_pipeline is not None and self._config.memory.query_pipeline
            # an unprompted tick retrieves too: urgent observations ask the
            # attention view on the proactive path
            force_attention = bool(
                not messages and observations and any(e.urgency >= 1 for e in observations)
            )
            if pipeline_on and (messages or observations):
                pipeline_result = await self._pipeline_context(
                    messages,
                    ctx,
                    observations,
                    recall_embed,
                    force_attention=force_attention,
                )
                if pipeline_result is not None:
                    recalled = pipeline_result.packed.trace
                    if pipeline_result.block:
                        recall_lines = [pipeline_result.block]
            else:
                if messages:
                    recalled, recall_lines = await self._recall_related(
                        messages, ctx, observations, recall_embed
                    )
            attention = (
                "" if pipeline_result is not None else await self._attention_context(messages)
            )
            context_block = self._format_context(ctx, attention=attention)
            delivery_block = "\n".join(delivery_lines)
            head = "\n\n".join(
                part for part in (context_block, delivery_block, "\n".join(recall_lines)) if part
            )
            if head:
                history.append(Message.user(f"[working context]\n{head}"))
            if messages:
                history.extend(await self._transcript_messages(messages))
            else:
                # a proactive turn reads the past as closed: the earlier
                # messages already had their turn, and a reply that answers
                # one of them again is the stale-answer bug
                closed = await self._transcript_messages([])
                if closed:
                    history.append(
                        Message.user(
                            "[closed history — earlier messages, each already "
                            "had its turn. Do not answer, restate or continue "
                            "any of them.]"
                        )
                    )
                    history.extend(closed)
            for event in reversed(observations):  # oldest first inside the block
                history.append(Message.user(f"[new event] {self._event_line(event)}"))
            for message in messages:
                line = f"[message from {message.handle} via {message.surface}]\n{message.text}"
                if message.reply_to_text:
                    # so the model can see *which* of its own messages is being
                    # answered when the platform hands the text over
                    line += f'\n(replying to my earlier message: "{message.reply_to_text[:200]}")'
                history.append(Message.user(line))
            if not messages:
                # the explicit instruction rides last, so the history ends on
                # a directive — never on an unanswered question
                history.append(
                    Message.user(
                        "Handle only the new event above. Do not answer, "
                        "restate or follow up on earlier messages."
                    )
                )
            history = _window_history(history, keep_head=1 if context_block else 0)
            calls: list[dict[str, Any]] = []  # filled by _execute as the turn runs
        else:
            # a retry continues the tool loop's own history — the context,
            # the user's words, and every tool result already collected.
            # Nothing is rebuilt, so a tool that already ran is never
            # proposed and executed a second time.
            history = list(resume.history)
            calls = resume.calls

        # a short ref this turn owns: the log lines below carry it, the
        # trace keeps it, so "why did you say that?" and the server log
        # meet on one searchable token — the panel renders named keys
        # only, so the ref never reaches a screen
        turn_ref = secrets.token_hex(3)
        trace: dict[str, Any] = {
            "turn": turn_ref,
            "trigger": {
                "messages": [
                    {
                        "surface": m.surface,
                        "handle": m.handle,
                        "text": m.text,
                        "reply_to_id": m.reply_to_id,
                        "reply_to_text": m.reply_to_text,
                    }
                    for m in messages
                ],
                "observations": [
                    {
                        "id": e.id,
                        "source": e.source,
                        "kind": e.kind,
                        "line": self._event_line(e),
                    }
                    for e in observations
                ],
                # what the turn kept in mind beyond its own window — same
                # shape as observations, so the panel's plain-language
                # rendering parses it unchanged
                "recalled": recalled,
            },
            "calls": calls,
        }
        if not messages:
            # exactly which outcome tools went to the model this turn: the
            # difference between "it was never offered" and "it did not call"
            trace["outcome_tools"] = sorted(offered_outcomes)
        if pipeline_result is not None and pipeline_result.packed.notes:
            trace["retrieval_notes"] = list(pipeline_result.packed.notes)
        if resume is not None:
            # a resumed attempt points at the dead one, so the record reads
            # as one ask with a gap, not two separate requests
            trace["resumed_from"] = resume.turn_ref
        full_history: list[Message] = []
        reply = ""
        internal_notes = ""
        internal_text = ""
        outcome: Any = None
        # Tool visibility is derived, never matched against prose. The core
        # tools ship with their schemas every turn; the prompt index names
        # everything else; `loaded` grows only through tools__load (or a
        # resumed attempt's own calls). The executor re-checks the same set,
        # so hiding a tool is not a security boundary — it is the shape of
        # the prompt.
        loaded: set[str] = set()
        if resume is not None:
            for entry in calls:
                name = entry.get("name")
                if not isinstance(name, str):
                    continue
                tool = self._tools.resolve(name)
                if tool is None:
                    continue
                if not tool.spec.core:
                    loaded.add(tool.spec.id)
                if tool.spec.id == "tools.load":
                    ids = (entry.get("params") or {}).get("ids")
                    if isinstance(ids, list):
                        accepted, _ = self._tools.load([str(i) for i in ids], loaded)
                        loaded.update(accepted)

        # the agent's own acts this turn is being told about: a tool whose
        # outcome just arrived. On a turn nobody asked for, re-proposing
        # one is a loop, not a new request — the executor refuses to park
        # it again (see _execute), so one carried-out ask never repeats.
        # A turn with a message in it never builds this map: the user
        # typing "send it again" is a fresh request and must be heard.
        settled: dict[str, str] = {}
        if not messages:
            for event in observations:
                if event.source != "agent" or not isinstance(event.payload, dict):
                    continue
                tool = event.payload.get("tool")
                if isinstance(tool, str) and tool:
                    params = event.payload.get("params")
                    settled[_settled_key(tool, params)] = str(
                        event.payload.get("plain") or describe_call(tool, params)
                    )

        def _gate(active: set[str], so_far: list[Message]) -> list[WireSpec]:
            # `active` is the loop's own record of what it already called;
            # every called tool was loaded first, so the loaded set is the
            # whole truth here.
            return self._tools.core_specs() + self._tools.schemas(loaded) + outcome_tools

        async def _execute_unlocking(call: ToolCall) -> ToolResult:
            if is_terminal_tool(call.name):
                accepted = canonical_tool(call.name) in offered_outcomes
                record_outcome(
                    calls,
                    call,
                    accepted=accepted,
                    plain=describe_call(call.name, call.arguments),
                )
                return ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    content=(
                        "outcome accepted" if accepted else "that action is not offered this turn"
                    ),
                    is_error=not accepted,
                )
            tool = self._tools.resolve(call.name)
            if tool is not None and tool.spec.id == "tools.load":
                raw = call.arguments.get("ids")
                if not isinstance(raw, list) or not raw:
                    content = "tools__load needs a list of tool names."
                    result = ToolResult(
                        tool_call_id=call.id, name=call.name, content=content, is_error=True
                    )
                    self._note_call(calls, call, result)
                    return result
                accepted, content = self._tools.load([str(i) for i in raw], loaded)
                loaded.update(accepted)
                result = ToolResult(tool_call_id=call.id, name=call.name, content=content)
                self._note_call(
                    calls,
                    call,
                    result,
                    ruling=Ruling(Decision.ALLOW, "builtin:tool-loader", "loaded on demand"),
                )
                return result
            if tool is not None and not tool.spec.core and tool.spec.id not in loaded:
                # the schema never reached the model, so a call for it is a
                # proposal for a tool this turn does not have — refuse it in
                # words instead of running it behind an empty prompt
                result = ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    content=(
                        "that tool isn't loaded this turn — find it with "
                        "tools__search and load it with tools__load first"
                    ),
                    is_error=True,
                )
                self._note_call(calls, call, result)
                return result
            return await self._execute(call, trace=calls, settled=settled)

        # "typing…" for as long as a solicited turn runs — the surface says
        # work is happening instead of minutes of silence, and the pulse
        # dies with the turn (the finally below) so a parked card is never
        # followed by a ghost indicator. Unprompted ticks never pulse:
        # nobody is waiting on an answer.
        typing: asyncio.Task[None] | None = None
        if messages:
            typing = asyncio.create_task(self._typing_pulse())
        # the loop writes its history here as it goes, so a provider failure
        # can resume from the exact step that died instead of starting over
        loop_history: list[Message] = []
        attempt = 1 if resume is None else resume.attempts + 1
        # where this turn came from. The harness decides; the model cannot
        # produce an owner origin from content it read. A solicited turn
        # carries the message ids it answers, sealed on any alert it writes;
        # an unprompted tick carries nothing, so it cannot mutate.
        origin_token = set_origin(
            owner_message(
                {
                    "chat_refs": sorted({m.chat_ref for m in messages if m.chat_ref}),
                    "message_ids": sorted({m.message_id for m in messages if m.message_id}),
                }
            )
            if messages
            else unprompted()
        )
        system = await self._system_prompt()
        system = f"{system}\n\n{self._tool_index(outcome_tools)}"
        try:
            # one turn's hard ceiling: a hung provider or a runaway tool
            # loop ends here (the wait cancels whatever was stuck) instead
            # of open-ended silence
            final, full_history = await asyncio.wait_for(
                run_tool_loop(
                    provider,
                    system,
                    history,
                    _gate,
                    _execute_unlocking,
                    max_iterations=self._config.agent.max_tool_iterations,
                    progress=loop_history,
                    terminal=TERMINAL_TOOLS,
                ),
                timeout=self._config.agent.turn_timeout_seconds,
            )
            # the typed outcome is the only door to the user. An answer turn
            # uses reply/ask; an unprompted turn uses notify/ask. Free text on
            # an unprompted turn is internal — unless a typed triage call
            # recognises it as a complete message for the person.
            outcome = outcome_from_calls(calls, solicited=bool(messages))
            # a solicited turn that ends in free text still reaches the person
            # (the retry/fallback below owns the empty case), but the miss is
            # recorded: the typed outcome is the contract, and the trace is
            # where a systematic miss shows up instead of hiding in prose
            if outcome is None and messages:
                trace["terminal_miss"] = True
            triaged = False
            if outcome is None and not messages:
                candidate = clean_outbound(final.text)
                if candidate:
                    decision = await self._triage_unprompted(provider, observations, candidate)
                    trace["triage"] = {
                        "delivered": decision.deliver,
                        "reason": decision.reason,
                    }
                    if decision.deliver:
                        outcome = Notify(candidate)
                        triaged = True
            if isinstance(outcome, Reply):
                reply = clean_outbound(outcome.text)
                internal_notes = outcome.internal_notes
            elif isinstance(outcome, Ask):
                if messages:
                    reply = render_ask(outcome, locale=self._config.agent.locale)
                else:
                    internal_text = clean_outbound(final.text)
            elif isinstance(outcome, Notify):
                if not triaged:
                    internal_text = clean_outbound(final.text)
            else:
                if messages:
                    reply = clean_outbound(final.text)
                else:
                    internal_text = clean_outbound(final.text)
            delivered = _delivered_by_tool(calls, self._tools)
            parked = _parked_this_turn(calls)
            reply_was_fallback = False
            if messages and not reply and not delivered and not parked:
                # a solicited turn that produced nothing usable is an agent
                # error: one retry with an explicit instruction, then one
                # short plain fallback — the person is never left with
                # silence, and the failure is recorded on the trace
                reply = await self._retry_empty_reply(provider, system, full_history)
                if not reply:
                    reply = EMPTY_REPLY
                    reply_was_fallback = True
                    trace["error"] = "empty model reply"
                    log.warning("turn %s produced no deliverable reply — fallback sent", turn_ref)
            # the verifier checks every memory claim in the reply against the
            # packed chunks and returns findings only. The composer resolves
            # each unsupported claim, and a value guard blocks internal ids
            # and tags. It only runs for a reply that will actually reach the
            # user — a tool that already delivered, or a parked approval card,
            # has no reply to check.
            verification: dict[str, Any] | None = None
            if (
                reply
                and messages
                and not reply_was_fallback
                and pipeline_result is not None
                and pipeline_result.packed.gate in ("ok", "no_relevant_memory")
                and not delivered
                and not parked
            ):
                reply, verification, compose_notes = await self._verified_reply(
                    provider,
                    reply,
                    full_history,
                    pipeline_result,
                    messages=messages,
                    locale=self._config.agent.locale,
                )
                if compose_notes:
                    internal_notes = "\n".join(
                        part for part in (internal_notes, compose_notes) if part
                    )
            if verification is not None:
                trace["verification"] = verification
            if messages:
                if reply and not delivered and not parked:
                    # the user spoke first — a reply is solicited, it never
                    # waits; record where it landed so a later "why?" can
                    # find this trace
                    outcomes: dict[str, bool] = {}
                    reply_to = self._outbound_reply_target(messages, records, force_quote)
                    refs = await self._surfaces.send_to_user(
                        reply, outcomes=outcomes, reply_to=reply_to
                    )
                    if refs:
                        trace["chat_refs"] = refs
                    # a delivered fallback for an empty model reply is still
                    # a failed inbound: the user got a notice, not an answer,
                    # and the question must not be re-answered later. A reply
                    # that reached no surface is the same failure.
                    landed = bool(refs) or any(outcomes.values())
                    await self._settle_inbound(
                        records,
                        answered=landed and not reply_was_fallback,
                        reason="empty model reply" if reply_was_fallback else "reply not delivered",
                        refs=refs,
                    )
                elif reply:
                    # the turn already ended in front of the user — a sending
                    # tool delivered, or an approval card is waiting on a tap;
                    # a final reply beside either reads as the same answer
                    # twice, differently worded, or as filler ("OK.") after
                    # the card. The reply stays in the trace, so replay still
                    # shows how the turn ended.
                    log.info(
                        "reply not sent — the turn already reached the user "
                        "(turn %s, delivered=%s parked=%s)",
                        turn_ref,
                        delivered,
                        parked,
                    )
                    await self._settle_inbound(records, answered=True)
            elif not delivered:
                # an unprompted turn reaches the user only through notify or
                # ask. Anything else the model wrote is internal. The only
                # brake left is quiet hours, which holds a low-urgency note
                # in _notify and batches it when the window ends.
                if isinstance(outcome, Notify):
                    if await self._proactive_reply_allowed(outcome.content):
                        refs = await self._notify(
                            outcome.content,
                            urgent=max((e.salience_score for e in observations), default=0.0),
                        )
                        if refs:
                            trace["chat_refs"] = refs
                    else:
                        trace["reply_blocked"] = True
                        log.info("proactive notify blocked (turn %s, stale replay)", turn_ref)
                elif isinstance(outcome, Ask):
                    # a question rides the same budget as a note, so quiet
                    # hours hold it and batch it rather than waking the user
                    question = render_ask(outcome, locale=self._config.agent.locale)
                    if await self._proactive_reply_allowed(question):
                        refs = await self._notify(
                            question,
                            urgent=max((e.salience_score for e in observations), default=0.0),
                        )
                        if refs:
                            trace["chat_refs"] = refs
                    else:
                        trace["reply_blocked"] = True
                        log.info("proactive ask blocked (turn %s, stale replay)", turn_ref)
        except TimeoutError:
            # the ceiling bit: a handled outcome, not a crash — the trace
            # keeps the fact, the user gets plain words, and the tick
            # goes on. No re-raise: nothing is left worth flagging.
            trace["error"] = f"turn timed out after {self._config.agent.turn_timeout_seconds:.0f}s"
            log.warning(
                "turn %s timed out after %.0fs",
                turn_ref,
                self._config.agent.turn_timeout_seconds,
            )
            if messages:
                await self._surfaces.send_to_user(TIMEOUT_REPLY)
            await self._settle_inbound(records, answered=False, reason="turn timed out")
        except LLMProviderError as exc:
            # a dead provider is a handled outcome: the user hears the
            # cause in plain words, and a retryable outage hands the turn
            # back to the loop to pick up when the service answers again
            resume_history = loop_history or history
            full_history = resume_history
            resumed = await self._handle_provider_failure(
                exc,
                messages=messages,
                observations=observations,
                history=resume_history,
                calls=calls,
                turn_ref=turn_ref,
                attempt=attempt,
                trace=trace,
                previous=resume.failure if resume is not None else None,
                records=records,
            )
            if not resumed:
                # no retry is coming — the turn failed for good
                await self._settle_inbound(records, answered=False, reason="provider failure")
        except Exception as exc:
            trace["error"] = f"{type(exc).__name__}: {exc}"
            if messages:
                # a solicited turn dying mid-flight must not leave the user
                # staring at silence — plain words, and send_to_user already
                # swallows per-surface failures so this can't double-fault.
                # Unprompted ticks stay quiet: a background hiccup is a log
                # line, never an interruption.
                await self._surfaces.send_to_user(FAILURE_REPLY)
            await self._settle_inbound(records, answered=False, reason="turn failed")
            raise
        finally:
            # the turn is over; a later worker call must read as context,
            # never as the user's own instruction
            reset_origin(origin_token)
            # the pulse dies with the turn, whatever ended it — awaited so
            # the cancelled task never outlives the turn as a dangling
            # pending warning at loop close
            if typing is not None:
                typing.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await typing
            # persist even when the turn died mid-flight: the calls that did
            # run are already in `calls` (filled by reference), so a partial
            # record still answers "what were you doing?"
            reasoning = [
                m.text.strip() for m in full_history if m.role == "assistant" and m.text.strip()
            ]
            if reasoning and reply and reasoning[-1] == reply:
                reasoning.pop()  # the reply is stored as its own field
            if reasoning and internal_text and reasoning[-1] == internal_text:
                reasoning.pop()  # internal text is stored as its own field
            if reasoning:
                trace["reasoning"] = reasoning
            if reply:
                trace["reply"] = reply
            if internal_notes:
                trace["internal_notes"] = internal_notes
            if internal_text:
                trace["internal"] = internal_text
            trace["outcome"] = (
                "reply"
                if isinstance(outcome, Reply)
                else "notify"
                if isinstance(outcome, Notify)
                else "ask"
                if isinstance(outcome, Ask)
                else "none"
            )
            await self._save_trace(
                kind=TURN,
                label=self._trace_label(messages, observations),
                payload=trace,
            )
            # the focus object and a state-of-now conclusion are updated with
            # the finished turn — a failure costs a reference, never
            # the reply that already went out
            try:
                if messages:
                    await self._update_focus(messages, observations, reply)
                if reply:
                    await self._record_state_conclusion(messages, reply)
            except Exception:
                log.warning("focus/conclusion update failed — continuing", exc_info=True)

    async def _retry_empty_reply(
        self, provider: Provider, system: str, history: list[Message]
    ) -> str:
        """One more ask when a solicited turn's model output was nothing
        usable. Tools are withheld, so the retry can only ever be words, not
        a fresh proposal; a second blank returns "" for the caller's
        fallback. The provider's own failure is not this method's business —
        it degrades to "" so the fallback still lands."""
        nudge = Message.user(
            "Your previous output was blank or a placeholder. Reply now, in "
            "one short plain sentence, to the person's last message above."
        )
        try:
            turn = await provider.complete(system, [*history, nudge], [])
        except Exception:
            log.warning("empty-reply retry failed", exc_info=True)
            return ""
        return clean_outbound(turn.text)

    def _outbound_reply_target(
        self,
        messages: list[InboundMessage],
        records: list[Any],
        force_quote: bool,
    ) -> str | None:
        """The platform message id a solicited reply should quote, or None.

        Default is a plain message. A quote is warranted only when the
        answer is about an earlier message rather than the latest one: the
        user replied to a specific message, several questions were pending,
        newer messages arrived while this was being prepared, or the answer
        was delayed past REPLY_DELAY_SECONDS. A prompt answer to the latest
        message is never a reply.
        """
        if not messages:
            return None
        message = messages[-1]
        if not message.message_id:
            return None
        if message.reply_to_id:
            return message.message_id
        if force_quote or not self._queue.empty():
            return message.message_id
        created = records[0].created_at if records else None
        if created is not None and (datetime.now(UTC) - created).total_seconds() >= (
            REPLY_DELAY_SECONDS
        ):
            return message.message_id
        return None

    async def _proactive_reply_allowed(self, reply: str) -> bool:
        """False when a proactive reply would replay a closed question or a
        message already sent to this chat.

        The transcript is the source of truth: an 'out' row that matches the
        reply is a duplicate sent inside the window; an 'in' row the reply
        mostly echoes is a closed question restated. With no transcript there
        is nothing to compare, and the reply is allowed — the interruption
        budget is the only remaining guard.
        """
        if self._transcript is None:
            return True
        target = _significant_tokens(reply)
        if not target:
            return False
        try:
            rows = await self._transcript.recent(limit=_TRANSCRIPT_MESSAGES)
        except Exception:
            log.warning("could not read the transcript for the replay guard", exc_info=True)
            return True
        cutoff = datetime.now(UTC) - timedelta(hours=_PROACTIVE_DUPLICATE_HOURS)
        closed: list[str] = []
        for row in rows:
            text = str(row.get("text", "")).strip()
            if not text:
                continue
            direction = row.get("direction")
            if direction == "out":
                when = row.get("created_at") or row.get("at")
                if isinstance(when, datetime) and when < cutoff:
                    continue
                if _significant_tokens(text) == target:
                    return False
            elif direction == "in":
                closed.append(text)
        for question in closed:
            qtokens = _significant_tokens(question)
            if not qtokens:
                continue
            if len(target & qtokens) / len(target) >= 0.6:
                return False
        return True

    async def _settle_inbound(
        self,
        records: list[Any],
        *,
        answered: bool,
        reason: str = "",
        refs: dict[str, str] | None = None,
    ) -> None:
        """Write the one terminal outcome for each inbound this turn owed.

        A blank or failed reply makes the inbound failed — it is never left
        'received' for a later tick to answer again. The outbound message id
        (or the fact that nothing reached a surface) is what "answered"
        records, so it is a recorded outcome, never 'the reply text was
        non-empty'.
        """
        for record in records:
            if record is None or getattr(record, "terminal", False):
                continue
            try:
                if answered:
                    await self._inbound_states.mark_answered(
                        record.id, (refs or {}).get(record.surface, "")
                    )
                else:
                    await self._inbound_states.mark_failed(record.id, reason)
            except Exception:
                log.warning(
                    "could not settle inbound %s", getattr(record, "id", "?"), exc_info=True
                )

    def _connected_apps(self) -> str:
        """The live MCP server list for the system prompt — the model sees
        exactly what's reachable, so "can you send emails?" gets an honest
        "mail isn't connected" instead of an invented capability."""
        servers = self._tools.mcp_servers()
        return ", ".join(servers) if servers else "none"

    async def _pipeline_context(
        self,
        messages: list[InboundMessage],
        ctx: Any,
        observations: list[Event],
        query_embed: asyncio.Task[list[float] | None] | None,
        *,
        force_attention: bool = False,
    ) -> Any:
        """Run the routed pipeline for this turn: resolve, retrieve, fuse,
        gate and pack. Returns None on failure — the turn continues with
        whatever base context it already has, exactly like the old recall."""
        if self._query_pipeline is None:
            return None
        chat_ref = messages[-1].chat_ref if messages else ""
        focus_state = None
        if self._focus is not None and chat_ref:
            try:
                focus_state = await self._focus.get(chat_ref)
            except Exception:
                log.warning("could not read the focus object for routing", exc_info=True)
        entity_names = await self._entity_names(focus_state)
        excluded = {e.id for e in ctx.events}
        excluded |= {e.id for e in observations if e.kind == "chat_message"}
        text = " ".join(
            [m.text for m in messages] + [m.reply_to_text for m in messages if m.reply_to_text]
        ).strip()
        if not text:
            # an unprompted tick retrieves too: its observations are the
            # question, and the proactive path answers from the views
            text = " ".join(
                (event.summary or _event_search_text(event)) for event in observations
            ).strip()
        if not text:
            return None
        try:
            return await self._query_pipeline.run(
                text,
                focus=focus_state,
                entity_names=entity_names,
                query_embed=query_embed,
                exclude_event_ids=excluded,
                owner_addresses=self._owner_addresses,
                force_attention=force_attention,
            )
        except Exception:
            log.exception("query pipeline failed — continuing without routed recall")
            return None

    async def _entity_names(self, focus_state: Any) -> dict[int, str]:
        """Display names for the focus object's people, so a name in the
        question can become an entity filter instead of a word."""
        names: dict[int, str] = {}
        if self._entities is None or focus_state is None:
            return names
        for entity_id in focus_state.recent_entities:
            try:
                entity = await self._entities.get(entity_id)
            except Exception:
                continue
            if entity is None:
                continue
            label = (entity.display_name or "").strip()
            if not label and entity.handles:
                label = str(entity.handles[0][1] or "").strip()
            if label:
                names[entity_id] = label
        return names

    async def _verified_reply(
        self,
        provider: Provider,
        reply: str,
        history: list[Message],
        pipeline_result: Any,
        *,
        messages: list[InboundMessage],
        locale: str = "en",
    ) -> tuple[str, dict[str, Any], str]:
        """Check the reply against the pack, compose a safe answer, and keep
        internal findings out of the words that leave.

        The verifier returns data only. The composer resolves each
        unsupported claim (remove, soften or ask). A value-based guard then
        blocks any finding id, source id, tag or internal label that
        survived; on a hit it regenerates once with the user-safe view and
        refuses delivery if the leak persists.
        """
        packed = pipeline_result.packed
        conversation = "\n".join(
            text
            for text in ((getattr(message, "text", "") or "").strip() for message in history[-24:])
            if text and text != reply
        )
        extra_corpus = conversation[-8_000:]
        try:
            findings = find_findings(
                reply,
                chunks_by_event=packed.chunks_by_event,
                known_event_ids=set(packed.event_ids),
                extra_corpus=extra_corpus,
            )
        except Exception:
            log.warning(
                "citation verification failed — sending the reply as written", exc_info=True
            )
            return reply, {"ok": False, "error": "verifier failed"}, ""
        unsupported_findings = unsupported(findings)
        record: dict[str, Any] = {
            "ok": not unsupported_findings,
            "findings": [finding.as_dict() for finding in findings],
        }
        internal_values = collect_internal_values(
            findings=findings,
            event_ids=packed.event_ids,
            extra_texts=[
                "[internal verification findings]",
                "verification feedback",
                "internal_notes",
            ],
        )
        guard = LeakGuard(internal_values)
        if not unsupported_findings:
            # nothing to resolve: the answer stands, with any bracketed id or
            # finding tag removed by value before it leaves
            text = strip_values(reply, internal_values)
            if text != reply:
                record["stripped"] = True
            return text, record, ""
        if not findings:
            return reply, record, ""

        system = await self._system_prompt()
        timeout = min(60.0, self._config.agent.turn_timeout_seconds)
        composed = None
        try:
            composed = await asyncio.wait_for(
                compose_answer(
                    provider,
                    system,
                    list(history),
                    findings=findings,
                    candidate=reply,
                ),
                timeout=timeout,
            )
        except Exception:
            log.warning("composer failed — using the deterministic fallback", exc_info=True)

        notes = ""
        if composed is not None and composed.reply:
            text = composed.reply
            notes = composed.internal_notes
            record["resolutions"] = dict(composed.resolutions)
            if "ask" in composed.resolutions.values():
                record["asked"] = True
            missing = unresolved(findings, composed.resolutions)
            if missing:
                text = deterministic_compose(
                    text, missing, locale=locale, internal_values=internal_values
                )
                record["deterministic"] = True
        else:
            text = deterministic_compose(
                reply, unsupported_findings, locale=locale, internal_values=internal_values
            )
            record["deterministic"] = True

        hits = guard.scan(text)
        if hits:
            # the composer echoed an internal value — record it and try once
            # with only the user-safe view
            record["leak"] = {
                "blocked": [hit.value for hit in hits],
                "regenerated": True,
            }
            log.info("verification leak blocked — regenerating once with the safe view")
            safe_history = [Message.user(message.text) for message in messages if message.text]
            safe = None
            try:
                safe = await asyncio.wait_for(
                    compose_answer(
                        provider,
                        system,
                        safe_history,
                        findings=[],
                        candidate="",
                        safe_only=True,
                    ),
                    timeout=timeout,
                )
            except Exception:
                log.warning("safe regeneration failed — using the catalog fallback", exc_info=True)
            if safe is not None and safe.reply:
                text = safe.reply
                notes = safe.internal_notes or notes
            else:
                text = render_message("verification.safe_fallback", locale=locale)
            remaining = guard.scan(text)
            if remaining:
                record["leak"]["still_leaking"] = [hit.value for hit in remaining]
                text = render_message("verification.safe_fallback", locale=locale)

        record["ok"] = not record.get("leak", {}).get("still_leaking")
        return text, record, notes

    async def _recall_related(
        self,
        messages: list[InboundMessage],
        ctx: Any,
        observations: list[Event],
        query_embed: asyncio.Task[list[float] | None] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """The memories a solicited turn pulls in automatically: chunks of
        older conversations and shared documents that relate to what was
        just asked — semantic, so a missing or failing embedding layer
        simply recalls nothing. `query_embed` is the head start _turn gave
        the query embedding so its round trip overlaps the context build;
        without one it is embedded here. Excludes what the turn already
        sees (working context events, this tick's chat observations), but a
        document shared this same tick stays retrievable — that is what
        makes "what's in slide 3" answerable in one turn. Failure costs
        the turn nothing: an empty recall."""
        if self._vectors is None or not self._vectors.enabled:
            return [], []
        query = " ".join(
            [m.text for m in messages] + [m.reply_to_text for m in messages if m.reply_to_text]
        ).strip()
        if not query:
            return [], []
        excluded = {e.id for e in ctx.events}
        excluded |= {e.id for e in observations if e.kind == "chat_message"}
        try:
            q = (
                await query_embed
                if query_embed is not None
                else await self._vectors.embed_query(query)
            )
            if q is None:
                return [], []
            hits = await self._vectors.search_vector(q, limit=12)
            # a document the user just named goes to the front, ahead
            # of the similarity ranking alone — "explain deadlock
            # prevention from OSY.pdf" must land in OSY.pdf
            lowered = query.lower()
            hits.sort(
                key=lambda h: (
                    0 if h.filename and Path(h.filename).stem.lower() in lowered else 1,
                    -h.score,
                )
            )
            candidates: list[tuple[int, str, str, str, str]] = []
            for hit in hits:
                candidates.append(
                    (hit.event_id, hit.source, hit.kind, await self._hit_header(hit), hit.text)
                )
            return self._render_recall(candidates, excluded)
        except Exception:
            log.exception("memory recall failed — continuing without it")
            return [], []

    async def _hit_header(self, hit: MemoryHit) -> str:
        """The citation half of a recalled chunk: which document and page,
        or who said it and when — never a bare blob of text."""
        when = f"{hit.occurred_at:%Y-%m-%d}"
        if hit.kind == "shared_doc":
            where = ", ".join(part for part in (hit.filename, hit.location) if part)
            return where or hit.source
        if hit.kind == "chat_message":
            handle = None
            try:
                event = await self._events.get(hit.event_id)
                if event and isinstance(event.payload, dict):
                    sender = event.payload.get("_sender")
                    if isinstance(sender, dict):
                        handle = sender.get("handle")
            except Exception:
                pass
            if handle:
                return f"@{handle} on {hit.source}, {when}"
        return f"{hit.source}, {when}"

    @staticmethod
    def _render_recall(
        candidates: list[tuple[int, str, str, str, str]], excluded: set[int]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Candidates in, capped prompt lines + trace entries out. The caps
        rank by relevance, they don't window the store: at most 3 chunks
        per source event, 6 lines, ~4,000 chars — so six documents can
        contribute without any one of them drowning the rest."""
        lines: list[str] = []
        trace: list[dict[str, Any]] = []
        per_event: dict[int, int] = {}
        chars = 0
        for event_id, source, kind, header, text in candidates:
            if event_id in excluded or per_event.get(event_id, 0) >= 3:
                continue
            line = f"[related memory — {header}] {text[:600]}".strip()
            if not line:
                continue
            if len(lines) >= 6 or chars + len(line) > 4000:
                break
            per_event[event_id] = per_event.get(event_id, 0) + 1
            chars += len(line)
            lines.append(line)
            trace.append({"id": event_id, "source": source, "kind": kind, "line": line[:200]})
        return trace, lines

    async def _transcript_messages(self, current: list[InboundMessage]) -> list[Message]:
        """The recent cross-surface dialogue as user/assistant messages —
        the turn's conversation memory. Rows echoing this turn's own inbound
        are dropped: they get the richer [message from …] line below. A
        missing or broken transcript costs the turn nothing."""
        if self._transcript is None:
            return []
        try:
            rows = await self._transcript.recent(limit=_TRANSCRIPT_MESSAGES)
        except Exception:
            log.exception("transcript read failed — continuing without it")
            return []
        fresh = {m.text for m in current}
        out: list[Message] = []
        for row in rows:
            text = row.get("text", "").strip()
            if not text:
                continue
            if row["direction"] == "out":
                out.append(Message.assistant(text))
            elif text not in fresh:
                out.append(Message.user(f"[{row.get('surface', '?')}] {text}"))
        return out

    def _format_context(self, ctx: Any, attention: str = "") -> str:
        lines: list[str] = []
        # the attention view, only when this turn's route asked for one —
        # it packs "urgent or due items if the route asked", and a live
        # result carries its own "as of" time
        if attention:
            lines.append(attention)
        for event in ctx.events[:15]:
            lines.append(f"- {self._event_line(event)}")
        for note in ctx.notes:
            who = note.payload.get("handle") or "someone"
            lines.append(f"- note on {who}: {note.payload.get('note', '')}")
        if ctx.pending_approvals:
            lines.append(
                f"- {len(ctx.pending_approvals)} approval(s) already presented to "
                "the user with one-tap buttons — do not ask, remind, or bring "
                "them up again; they run (or quietly don't) the moment the "
                "user decides"
            )
        # one-off questions to put to the user this turn, in the words the
        # context builder chose — currently the owner-alias confirmation
        for question in ctx.questions:
            lines.append(f"- {question}")
        if ctx.today_memorable:
            lines.append(f"- {ctx.today_memorable} memorable event(s) so far today")
        return "\n".join(lines)

    def _event_line(self, event: Event) -> str:
        """One event as the model reads it.

        A mail gets its own rendering — raw headers, display names, and
        "(your other address)" for an owner address — instead of a JSON dump
        that loses who said what. Everything else keeps the payload preview
        the salience judge has always scored.
        """
        line = ""
        if event.kind == MAIL_KIND:
            try:
                line = render_mail_event(
                    event.payload,
                    occurred_at=event.occurred_at,
                    salience=event.salience_score,
                    owners=self._owner_addresses,
                )
            except Exception:  # a malformed payload falls back to the dump
                log.warning("mail rendering failed for event %s — using the raw line", event.id)
        if not line:
            text = json.dumps(event.payload, ensure_ascii=False, default=str)
            line = (
                f"{event.occurred_at:%Y-%m-%d %H:%M} {event.source}/{event.kind}"
                f" (salience {event.salience_score:.0f}): {text[:400]}"
            )
        labels = event.meta.get("alert_matches") if isinstance(event.meta, dict) else None
        if isinstance(labels, list):
            names = ", ".join(
                f'"{str(label).replace(chr(10), " ").strip()[:80]}"'
                for label in labels[:3]
                if label
            )
            if names:
                # context for the model, not a message: the agent decides in
                # its own words whether this is worth saying
                line += f" — an alert matched this ({names})"
        return line

    async def _refresh_owner_addresses(self) -> None:
        """Cache the owner's own addresses for this turn's mail rendering."""
        if self._entities is None:
            return
        try:
            self._owner_addresses = await self._entities.owner_addresses()
        except Exception:
            log.warning("could not read the owner addresses — rendering without them")

    async def _await_enrichment(self, events: list[Event]) -> None:
        """Give the events this turn reads a short, bounded head start.

        The floor already made an event visible at ingest, so this is
        detail — a summary, a deadline, a classifier urgency — not the thing
        that makes the event exist. A turn waits only for the events it is
        about to read, never for unrelated work, and a deadline that expires
        stops the wait without cancelling the enrichment.
        """
        wait = getattr(self._events, "wait_for_events", None)
        if wait is None:
            return
        event_ids = [event.id for event in events]
        if not event_ids:
            return
        try:
            still = await wait(event_ids, timeout=ENRICHMENT_WAIT_SECONDS)
        except Exception:
            log.warning("could not wait for enrichment — answering from the floor", exc_info=True)
            return
        if still:
            log.info(
                "enrichment still in flight after %.1fs for %d event(s) — answering from the floor",
                ENRICHMENT_WAIT_SECONDS,
                len(still),
            )

    # -- structured views, focus and delivery ---------------------------------

    async def _attention_context(self, messages: list[InboundMessage]) -> str:
        """The attention view for a turn whose words asked for it.

        Only when the deterministic route trigger fired — an unprompted tick
        and a normal question never carry it — and it says plainly that it is
        a live result, "as of" the moment it was read.
        """
        if self._views is None or not messages:
            return ""
        if not any(wants_attention(message.text) for message in messages):
            return ""
        now = datetime.now(UTC)
        items = self._views.pending_attention(now=now, limit=20)
        body = self._views.render_attention(items, now=now)
        stamp = _local_clock(now, self._config.memory.default_timezone)
        lead = f"- attention (live, as of {stamp})"
        try:
            open_items = await self._views.commitments_view(limit=10)
            commitments = self._views.render_commitments(open_items, now=now)
        except Exception:
            commitments = ""
        blocks = [body, commitments]
        rendered = "\n".join(block for block in blocks if block)
        if not rendered:
            return f"{lead}: nothing urgent or due right now"
        indented = "\n".join(f"  {line}" for line in rendered.splitlines())
        return f"{lead}:\n{indented}"

    async def _redeliver_failed(self) -> list[str]:
        """Re-deliver a reply that never reached the user, and say so.

        The next turn is told "previous reply not delivered" with
        facts, and re-delivers. The original failure is already recorded as
        an event by `_record_delivery_notices`; this re-sends the exact words
        once, then abandons the row so a permanent outage does not repeat the
        same old reply on every later turn.
        """
        if self._deliveries is None:
            return []
        try:
            pending = await self._deliveries.undelivered(limit=5)
        except Exception:
            log.warning("could not read failed deliveries", exc_info=True)
            return []
        lines: list[str] = []
        for delivery in pending:
            if delivery.status != FAILED or not delivery.body.strip():
                continue
            try:
                outcomes: dict[str, bool] = {}
                refs = await self._surfaces.send_to_user(
                    delivery.body, record=False, outcomes=outcomes
                )
            except Exception:
                refs = {}
                outcomes = {}
            if refs or any(outcomes.values()):
                try:
                    await self._deliveries.mark_sent(delivery.id)
                except Exception:
                    log.warning("could not mark delivery %s sent", delivery.id)
                lines.append(
                    "- a previous reply did not reach the user when first sent; "
                    "it has now been re-delivered. Do not repeat it unless asked."
                )
            else:
                try:
                    await self._deliveries.mark_abandoned(delivery.id, "re-delivery failed")
                except Exception:
                    log.warning("could not mark delivery %s abandoned", delivery.id)
                lines.append(
                    "- a previous reply never reached the user and re-delivery "
                    "failed; do not claim it was delivered."
                )
        return lines

    async def _expire_stale_inbounds(self) -> None:
        """Never answer a message that has been waiting too long.

        A message still 'received' past PENDING_MAX_AGE_SECONDS is marked
        expired and gets at most one plain "still want this?" check, so a
        stale question is never answered minutes later as if it were fresh.
        The rows are read from the ledger, never inferred from the
        transcript.
        """
        cutoff = datetime.now(UTC) - timedelta(seconds=PENDING_MAX_AGE_SECONDS)
        try:
            stale = await self._inbound_states.stale_unchecked(cutoff)
        except Exception:
            log.warning("could not read stale inbounds", exc_info=True)
            return
        if not stale:
            return
        surfaces: set[str] = set()
        for row in stale:
            try:
                await self._inbound_states.mark_checked(row.id)
                await self._inbound_states.mark_expired(row.id)
            except Exception:
                log.warning("could not expire inbound %s", row.id, exc_info=True)
                continue
            surfaces.add(row.surface)
        if surfaces:
            # one check, not a drip — and enough to say it exists without
            # guessing which surface it came from
            await self._notify(
                "One message you sent earlier is still unanswered — send it "
                "again if you still want an answer."
            )

    async def _record_delivery_notices(self) -> None:
        """Turn any undelivered reply into an event, once per delivery.

        The transcript is not what an unprompted tick reads, and
        `_last_event_id` is what the tick's observation stream is keyed on —
        so an event is the only form that survives into every turn type.
        Dedup makes the notice durable exactly once: re-ingesting
        the same delivery is a duplicate and writes nothing.
        """
        if self._deliveries is None:
            return
        try:
            pending = await self._deliveries.undelivered(limit=5)
        except Exception:
            log.warning("could not read undelivered replies", exc_info=True)
            return
        for delivery in pending:
            try:
                await self._events.ingest(
                    source="delivery",
                    kind="delivery_failed",
                    payload={
                        "delivery_id": delivery.id,
                        "surface": delivery.surface,
                        "status": delivery.status,
                        "text": delivery.body[:400],
                        "at": delivery.created_at.astimezone(UTC).isoformat(),
                    },
                    occurred_at=delivery.created_at,
                    dedup_key=f"delivery:{delivery.id}",
                )
            except Exception:
                log.warning("could not record a delivery notice", exc_info=True)

    async def _update_focus(
        self, messages: list[InboundMessage], observations: list[Event], reply: str
    ) -> None:
        """Persist what the finished turn was about.

        This layer maintains the object only: the reference resolution that
        reads it belongs to the query router. It is per chat, so an
        unprompted tick with no chat reference updates nothing.
        """
        if self._focus is None or not messages:
            return
        chat_ref = messages[-1].chat_ref
        if not chat_ref:
            return
        entities = await self._focus_entities(messages, observations)
        offer = None
        if reply and reply.rstrip().endswith("?"):
            offer = {
                "text": reply[:400],
                "options": [],
                "at": datetime.now(UTC).isoformat(),
            }
        open_task: dict[str, Any] | None = None
        if self._views is not None:
            try:
                open_items = await self._views.commitments_view(limit=1)
            except Exception:
                open_items = []
            if open_items:
                first = open_items[0]
                open_task = {
                    "commitment_id": first.id,
                    "summary": first.text[:200],
                    "due_at": first.due_at.astimezone(UTC).isoformat() if first.due_at else None,
                }
        await self._focus.record_turn(
            chat_ref,
            entities=entities,
            offer=offer,
            open_task=open_task,
            attachment=_newest_attachment(observations),
        )

    async def _focus_entities(
        self, messages: list[InboundMessage], observations: list[Event]
    ) -> list[int]:
        """The identities this turn named, newest event first."""
        found: list[int] = []
        for event in reversed(observations):
            for entity_id in event.entity_ids:
                if entity_id not in found:
                    found.append(entity_id)
        if self._entities is not None:
            for message in messages:
                try:
                    entity = await self._entities.resolve_exact(message.surface, message.handle)
                except Exception:
                    entity = None
                if entity is not None and entity.id not in found:
                    found.append(entity.id)
        return found

    async def _record_state_conclusion(self, messages: list[InboundMessage], reply: str) -> None:
        """Cache a state-of-now answer with its `as_of` and a short TTL.

        The deterministic route trigger is the keyword set; the value is what
        Aether just answered, source "live". The query router reads it with
        `get_fresh`, so an expired answer is never served as current.
        """
        if self._conclusions is None or not messages or not reply.strip():
            return
        asked = " ".join(message.text for message in messages).strip()
        if not wants_attention(asked):
            return
        key = "now:" + " ".join(asked.lower().split())
        try:
            await self._conclusions.set(key, reply.strip(), source="live")
        except Exception:
            log.warning("could not record a state-of-now conclusion", exc_info=True)

    # -- the authorization-gated executor ---------------------------------------------

    @staticmethod
    def _note_call(
        trace: list[dict[str, Any]] | None,
        call: ToolCall,
        result: ToolResult,
        *,
        ruling: Any = None,
        audit_seq: int | None = None,
        approval_id: int | None = None,
    ) -> None:
        """Append one call's full record to the trace being built, if any —
        the gate's own view: the ruling, the audit row (or approval) it
        produced, and what came back. `trace is None` means the call isn't
        part of a traced act; tracing stays out of the decision path."""
        if trace is None:
            return
        if ruling is None:  # the call failed before the gate produced an outcome
            decision, matched_rule, reason = "error", "", ""
        else:
            decision = ruling.decision.value
            matched_rule = ruling.matched_rule
            reason = ruling.reason
        trace.append(
            {
                "id": call.id,
                "name": call.name,
                # the model's own sentence when it offered one, the backend's
                # vocabulary otherwise — the panel never needs the raw name
                "plain": call.plain or describe_call(call.name, call.arguments),
                "params": dict(call.arguments),
                "decision": decision,
                "matched_rule": matched_rule,
                "reason": reason,
                "audit_seq": audit_seq,
                "approval_id": approval_id,
                "result": result.content,
                "is_error": result.is_error,
            }
        )

    def _plain_unavailable(self, name: str) -> str:
        """Plain words for a name the namespace can't run — the model relays
        these to the user verbatim, so no internal names, no error jargon.
        The trace and audit keep the exact call for replay."""
        server, _, tool_name = name.partition("__")
        if not tool_name:
            return "that action isn't available right now"
        if self._tools.has_server(server):
            return f"that action isn't available in {server} right now"
        return f"{server} isn't connected right now"

    @staticmethod
    def _plain_unresponsive(name: str) -> str:
        """Plain words for a linked app that failed to answer a call."""
        server, sep, _ = name.partition("__")
        if sep:
            return f"{server} isn't responding right now"
        return "that app isn't responding right now"

    async def _execute(
        self,
        call: ToolCall,
        trace: list[dict[str, Any]] | None = None,
        origin: str | None = None,
        settled: dict[str, str] | None = None,
    ) -> ToolResult:
        """The single choke point between a proposal and the world. `trace`,
        when given, is the call-record list of the decision trace being
        built — passed explicitly because the scheduler worker and the agent
        loop run as separate tasks and must never write to a shared one.
        `origin`, when given, is the loop-internal marker for a change the
        user just drove in chat — it never rides a model proposal, so the
        policy can let a walk-driven connect through without widening
        anything else. `settled`, when given, maps a tool name to the plain
        sentence of the outcome this turn was just told about — an
        unprompted turn passes it so a re-proposal of an act that already
        ran is refused instead of parked again.

        A call the namespace cannot run never parks: the user is not asked
        to consent to certain failure — it comes back in the same plain
        words as any other missing tool."""
        # the model's one-sentence account of the call rides in as `_plain`
        # (see PLAIN_PARAM): lift it onto the record and hand the policy,
        # the audit, and the tool a call that never carries it
        plain = call.arguments.get(PLAIN_PARAM)
        # reserved keys are wiring, never model arguments: `_plain` becomes
        # the trace's sentence, and `_approved` is added by the approval
        # path only. A model can never smuggle either through a call.
        arguments = {k: v for k, v in call.arguments.items() if not k.startswith("_")}
        call = ToolCall(
            call.id, call.name, arguments, plain=str(plain) if plain is not None else ""
        )
        ruling = self._policy.classify(
            call.name,
            call.arguments,
            origin=origin,
            effect=(
                self._tools.get(call.name).effect
                if self._tools.get(call.name) is not None
                else None
            ),
            internal=self._tools.is_internal(call.name),
            aliases=self._tools.aliases_for(call.name),
        )

        # memory_search stays a model tool, capped per turn: the third round
        # is the last one, so a model that keeps rephrasing cannot loop the
        # same question through retrieval for the whole turn budget
        tool = self._tools.get(call.name)
        if tool is not None and tool.spec.id == "memory.search" and trace is not None:
            used = 0
            for entry in trace:
                found = self._tools.get(str(entry.get("name") or ""))
                if found is not None and found.spec.id == "memory.search":
                    used += 1
            if used >= self._config.memory.max_search_rounds:
                result = ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    content=(
                        "memory_search has been used the maximum number of times "
                        "this turn — answer from the memory already in front of you."
                    ),
                    is_error=True,
                )
                self._note_call(trace, call, result)
                return result

        if ruling.decision is Decision.DENY:
            seq = await self._audit.append(
                actor="agent",
                tool_name=call.name,
                decision="deny",
                rules_matched=ruling.matched_rule,
                params=call.arguments,
                outcome=ruling.reason,
            )
            result = ToolResult(
                tool_call_id=call.id,
                name=call.name,
                content=f"denied by policy: {ruling.reason}",
                is_error=True,
            )
            self._note_call(trace, call, result, ruling=ruling, audit_seq=seq)
            return result

        if ruling.decision is Decision.ALLOW:
            # the same allowed call proposed twice in one turn returns the
            # first result instead of running again — a doubled send is
            # how one request became two identical replies. The gate still
            # rules and the repeat is still recorded; only the second
            # execution is skipped, and failures stay retryable (an
            # errored first call never satisfies the repeat).
            if trace:
                for entry in trace:
                    if (
                        entry.get("name") == call.name
                        and entry.get("params") == call.arguments
                        and entry.get("decision") == "allow"
                        and not entry.get("is_error")
                    ):
                        result = ToolResult(
                            tool_call_id=call.id,
                            name=call.name,
                            content=str(entry.get("result", "")),
                        )
                        self._note_call(trace, call, result, ruling=ruling)
                        return result
            try:
                content = await self._tools.execute(call.name, call.arguments)
            except UnknownToolError:
                result = ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    content=self._plain_unavailable(call.name),
                    is_error=True,
                )
                self._note_call(trace, call, result, ruling=ruling)
                return result
            except ConnectorUnavailableError:
                result = ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    content=self._plain_unresponsive(call.name),
                    is_error=True,
                )
                self._note_call(trace, call, result, ruling=ruling)
                return result
            except ProviderError as exc:
                seq = await self._audit.append(
                    actor="agent",
                    tool_name=call.name,
                    decision="error",
                    rules_matched=ruling.matched_rule,
                    params=call.arguments,
                    outcome="provider error — see the turn trace",
                )
                result = ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    content=exc.plain,
                    is_error=True,
                )
                self._note_call(trace, call, result, ruling=ruling, audit_seq=seq)
                return result
            seq = await self._audit.append(
                actor="agent",
                tool_name=call.name,
                decision="allow",
                rules_matched=ruling.matched_rule,
                params=call.arguments,
                outcome=ruling.reason,
            )
            result = ToolResult(tool_call_id=call.id, name=call.name, content=content)
            self._note_call(trace, call, result, ruling=ruling, audit_seq=seq)
            return result

        # a risky call the namespace can't run must never ask for consent —
        # approving certain failure is not a decision worth interrupting the
        # user for. Answer with the same plain words the ALLOW path uses;
        # the trace keeps the exact call.
        if self._tools.get(call.name) is None:
            result = ToolResult(
                tool_call_id=call.id,
                name=call.name,
                content=self._plain_unavailable(call.name),
                is_error=True,
            )
            self._note_call(trace, call, result, ruling=ruling)
            return result

        # a call this unprompted turn was just told the outcome of: it
        # already ran (or was already answered) and the user heard the
        # result. Parking it again is how one approved send becomes a
        # loop of identical asks — the guard is code, because the parked
        # result's "do not propose this call again" only lives in the
        # turn that parked it, which a later turn never sees again.
        if settled:
            key = _settled_key(call.name, call.arguments)
            if key in settled:
                result = ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    content=(f"already settled — {settled[key]}. Do not propose it again."),
                )
                self._note_call(trace, call, result, ruling=ruling)
                return result

        # the same call parked earlier this turn: the model repeating
        # itself must not put the same question in front of the user
        # twice — the second proposal gets the first's held answer
        if trace:
            for entry in trace:
                if (
                    entry.get("approval_id") is not None
                    and entry.get("name") == call.name
                    and entry.get("params") == call.arguments
                ):
                    result = ToolResult(
                        tool_call_id=call.id,
                        name=call.name,
                        content=str(entry.get("result", "")),
                    )
                    self._note_call(trace, call, result, ruling=ruling)
                    return result

        # the same call already parked and still waiting: one request means
        # one question in front of the user — a second row for the same
        # ask is how one approved send later becomes two
        existing = await self._approvals.find_pending(call.name, call.arguments)
        if existing is not None:
            result = ToolResult(
                tool_call_id=call.id,
                name=call.name,
                content=(
                    f"already waiting for the user (#{existing.id}) — the same "
                    "request is in front of them with one-tap buttons. Do not "
                    "propose it again."
                ),
            )
            self._note_call(trace, call, result, ruling=ruling, approval_id=existing.id)
            return result

        # REQUIRE_APPROVAL: park it, ask, and let the turn go on — but a
        # call that names a stored file never parks. An approval must only
        # ever be asked for an action that can run exactly as offered: a
        # missing or unreadable file can't run (there is nothing to
        # attach), and a present one can't run either — the connected
        # actions take parameters, not bytes, so the file would silently
        # stay behind while "sent" got said. Both are refused in plain
        # words before any row is created; the model relays the reason, or
        # proposes the action again without the file if the user wants it.
        media_id = call.arguments.get("media_id") if isinstance(call.arguments, dict) else None
        if isinstance(media_id, bool) or not isinstance(media_id, int) or media_id < 1:
            media_id = None
        if media_id is not None:
            record = await self._media.get(media_id) if self._media is not None else None
            if record is None:
                why = (
                    f"not proposed: the file it would carry (media id {media_id}) is "
                    "no longer stored — it may have been removed to make room for "
                    "newer files, or that id was never a stored file. Tell the user "
                    "plainly and ask them to send the file again."
                )
            else:
                why = (
                    "not proposed: stored files can't yet be attached to that "
                    "action — it would run without the file. If the user still "
                    "wants the action, propose the call again without media_id "
                    "and say plainly that the file will stay behind."
                )
            result = ToolResult(tool_call_id=call.id, name=call.name, content=why)
            self._note_call(trace, call, result, ruling=ruling)
            return result
        approval = await self._approvals.create(
            tool_name=call.name,
            params=call.arguments,
            rules_matched=ruling.matched_rule,
            note=ruling.reason,
        )
        # the card is the ask itself — the action as a question, then the
        # request's own fields in their own order; the gate's reason is
        # wiring the user never reads
        ask = describe_ask(call.name, call.arguments)
        await self._surfaces.present_approval(approval.id, ask)
        result = ToolResult(
            tool_call_id=call.id,
            name=call.name,
            content=(
                f"held for approval (#{approval.id}) — the user has been asked on "
                "their chat surfaces and the web panel; it will run if they "
                "approve. Do not propose this call again. The request is already "
                "in front of the user with one-tap buttons — do not announce or "
                "narrate the wait in your reply."
            ),
        )
        self._note_call(trace, call, result, ruling=ruling, approval_id=approval.id)
        return result

    # -- decisions ------------------------------------------------------------------

    async def decide(self, approval_id: int, decision: str) -> Approval | None:
        """The web panel's path: decide, then carry out a fresh approval."""
        decision = {"approve": APPROVED, "deny": DENIED}.get(decision, decision)
        approval = await self._approvals.decide(approval_id, decision)
        if approval is not None:
            await self._carry_out(approval)
        return approval

    async def execute_decision(self, approval_id: int, decision: str) -> None:
        """The chat surfaces' path: they already persisted the decision via
        Approvals.decide; this only carries a fresh one out."""
        approval = await self._approvals.get(approval_id)
        if approval is None or approval.status not in (APPROVED, DENIED):
            return
        await self._carry_out(approval)

    async def _carry_out(self, approval: Approval) -> None:
        if approval.status == DENIED:
            log.info("approval %s denied — nothing ran", approval.id)
            await self._remember_outcome(approval, "action_denied", "denied by the user")
            await self._acknowledge_decision(
                approval,
                outcome=(
                    "The user denied it — nothing ran. Acknowledge the denial in "
                    "your own words; do not argue the case for the action."
                ),
                fallback=describe_dismissed(approval.tool_name, approval.params),
            )
            return
        trace: dict[str, Any] = {
            "approval_id": approval.id,
            "tool": approval.tool_name,
            "params": dict(approval.params),
            "decided_by": approval.decided_by,
        }
        try:
            run_params = dict(approval.params)
            with use_origin(approved_call(approval.id)):
                result = await self._tools.execute(approval.tool_name, run_params)
        except UnknownToolError as exc:
            # rows parked before the up-front check — or an app unlinked
            # while one sat pending — still land here. The user hears plain
            # words; the exact name and error stay in the trace and the log.
            log.warning("approved call %s can't run: %s", approval.tool_name, exc)
            await self._approvals.mark_failed(approval.id)
            trace["result"] = str(exc)
            trace["is_error"] = True
            outcome = (
                "The user approved it, but it could not run — "
                f"{self._plain_unavailable(approval.tool_name)}. Tell them plainly "
                "it did not happen."
            )
            fallback = (
                f"the approved action couldn't run — {self._plain_unavailable(approval.tool_name)}"
            )
        except ConnectorUnavailableError as exc:
            log.warning("approved call %s can't run: %s", approval.tool_name, exc)
            await self._approvals.mark_failed(approval.id)
            trace["result"] = str(exc)
            trace["is_error"] = True
            outcome = (
                "The user approved it, but it could not run — "
                f"{self._plain_unresponsive(approval.tool_name)}. Tell them plainly "
                "it did not happen."
            )
            fallback = (
                f"the approved action couldn't run — {self._plain_unresponsive(approval.tool_name)}"
            )
        except ProviderError as exc:
            log.warning("approved provider call %s can't run: %s", approval.tool_name, exc)
            await self._approvals.mark_failed(approval.id)
            trace["result"] = exc.plain
            trace["is_error"] = True
            outcome = (
                "The user approved it, but it could not run — "
                f"{exc.plain}. Tell them plainly it did not happen."
            )
            fallback = f"the approved action couldn't run — {exc.plain}"
        except Exception as exc:
            log.exception("approved call %s failed to run", approval.tool_name)
            await self._approvals.mark_failed(approval.id)
            trace["result"] = str(exc)
            trace["is_error"] = True
            outcome = (
                "The user approved it, but it failed unexpectedly — say so plainly "
                "and give the reason in your own words. The error that came back: "
                f"{exc}"
            )
            fallback = (
                "the approved action couldn't run — it failed unexpectedly. "
                "The decision record has exactly what happened."
            )
        else:
            # a flattened "error: ..." answer is a failure wearing a
            # success's shape — tool errors come back as strings, and this
            # is the one place that used to read them as confirmation.
            # "Sent" is only said when the action truly ran.
            flat = str(result).strip()
            if flat.startswith("error:"):
                await self._approvals.mark_failed(approval.id)
                log.warning(
                    "approved call %s reported an error: %s", approval.tool_name, flat[:200]
                )
                trace["result"] = result
                trace["is_error"] = True
                outcome = (
                    "The user approved it, but the service reported an error, so it "
                    "was not completed — say so plainly and give the reason in your "
                    f"own words. The service's error: {flat}"
                )
                fallback = (
                    "the approved action didn't go through — the service reported "
                    "an error, so it was not completed. The decision record has "
                    "exactly what happened."
                )
            else:
                await self._approvals.mark_executed(approval.id)
                log.info("approval %s executed", approval.id)
                trace["result"] = result
                trace["is_error"] = False
                outcome = f"The user approved it and it ran. The action's own report: {result}"
                # plain words only — the exact result stays in the decision
                # record and the trace; a raw dump never reaches the user
                fallback = describe_outcome(approval.tool_name, approval.params)
        await self._remember_outcome(
            approval,
            "action_failed" if trace["is_error"] else "action_done",
            str(trace["result"]),
        )
        await self._save_trace(
            kind=CARRY_OUT,
            label=f"approval #{approval.id} — {approval.tool_name}",
            payload=trace,
        )
        # the record is complete — only now does anyone speak
        await self._acknowledge_decision(approval, outcome=outcome, fallback=fallback)

    async def _acknowledge_decision(self, approval: Approval, outcome: str, fallback: str) -> None:
        """One reply about the decision the user just made. With a
        reasoning provider it is the model's own line — what ran, what
        failed with its real reason, or the denial — and the call is
        spawned rather than inline, so the tap that decided returns as
        fast as it always did. Without a provider, or when the turn dies
        or comes back empty, the plain fallback line lands instead: a
        user who just acted never gets silence."""
        provider = self._providers.for_role("reasoning") if self._providers else None
        if provider is None:
            await self._surfaces.send_to_user(fallback)
            return
        self._decision_reply = asyncio.create_task(
            self._model_acknowledgment(provider, approval, outcome, fallback)
        )

    async def _model_acknowledgment(
        self, provider: Provider, approval: Approval, outcome: str, fallback: str
    ) -> None:
        """The spawned exchange behind _acknowledge_decision: one
        complete() under the turn ceiling, no tools, then the reply
        straight to the user's chat surfaces — the same direct path the
        turn's own replies take, so quiet hours never swallow it."""
        ask = describe_ask(approval.tool_name, approval.params)
        try:
            turn = await asyncio.wait_for(
                provider.complete(
                    await self._system_prompt(),
                    [Message.user(_decision_ack_instruction(ask, outcome))],
                    [],
                ),
                timeout=self._config.agent.turn_timeout_seconds,
            )
            text = (turn.text or "").strip()
        except Exception:
            log.exception("the reply to a decision failed — falling back to plain words")
            text = ""
        await self._surfaces.send_to_user(text or fallback)

    async def _remember_outcome(self, approval: Approval, kind: str, detail: str) -> None:
        """Close the loop in memory. The model's only picture of past turns
        is the event store — every turn rebuilds its history from memorable
        events — and nothing else records the agent's *own* acts: an
        approved action's outcome used to reach only the chat surface and
        the trace store, neither of which a later turn ever reads, so the
        request kept looking open ("which mail did you mean?") hours after
        it ran. Recording must never break the act itself, so a failed
        write is a log line, not a raise."""
        try:
            ingest = await self._events.ingest(
                source="agent",
                kind=kind,
                payload={
                    "tool": approval.tool_name,
                    "params": dict(approval.params),
                    "plain": plain_outcome(approval.tool_name, approval.params, kind),
                    "detail": detail[:1000],
                    # unique per approval, so two identical sends both record
                    "approval_id": approval.id,
                },
            )
        except Exception:
            log.warning("couldn't record the %s outcome", kind, exc_info=True)
            return
        if ingest.stored and ingest.event_id is not None:
            await self._salience.score_event(ingest.event_id)

    # -- scheduled actions ------------------------------------------------------------

    async def execute_scheduled(self, action: ScheduledAction) -> str:
        """What the scheduler worker runs for each due row. The payload is a
        tool call — and it passes the same gate as the model's proposals."""
        payload = action.payload or {}
        if payload.get("type") != "tool":
            raise ValueError(f"unknown scheduled payload type: {payload.get('type')!r}")
        call = ToolCall(
            id=f"sched-{action.id}",
            name=str(payload.get("tool", "")),
            arguments=dict(payload.get("params") or {}),
        )
        calls: list[dict[str, Any]] = []
        with use_origin(scheduled(action.id)):
            result = await self._execute(call, trace=calls)
        # recorded before any raise, so a failed fire still has its trace
        await self._save_trace(
            kind=SCHEDULED,
            label=action.label,
            payload={
                "action": {
                    "id": action.id,
                    "label": action.label,
                    "run_at": f"{action.run_at:%Y-%m-%d %H:%M:%S%z}",
                },
                "calls": calls,
                "result": result.content,
                "is_error": result.is_error,
            },
        )
        if result.is_error:
            raise RuntimeError(result.content)
        return result.content

    async def execute_tool(self, call: ToolCall) -> str:
        """One tool call through the full gate — policy, audit, approval
        parking — for a caller that is not the model: the background
        worker's skills. Returns the tool's content, parked or not, so a
        skill can tell a staged action from a settled one by its words
        (no trace is kept; the proactive task's own row is the record)."""
        result = await self._execute(call)
        return result.content

    # -- traces -------------------------------------------------------------------

    async def _save_trace(self, *, kind: str, label: str, payload: dict[str, Any]) -> None:
        """Persist one trace. Traces are bookkeeping, never a participant:
        if the store isn't wired or the write fails, the act the trace
        records has already happened and stays untouched."""
        if self._traces is None:
            return
        try:
            await self._traces.create(kind=kind, label=label, payload=payload)
        except Exception:
            log.exception("failed to persist %s trace — the act stands", kind)

    @staticmethod
    def _trace_label(messages: list[InboundMessage], observations: list[Event]) -> str:
        """One human line for the trace list: the message that drove the
        turn, or what was seen if the turn was observation-only."""
        if messages:
            return messages[0].text[:48]
        return f"observed {len(observations)} new event(s)"
