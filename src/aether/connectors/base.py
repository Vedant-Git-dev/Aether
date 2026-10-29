"""Connector contracts shared by every surface Aether talks through."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..authz.approvals import EXPIRED, PENDING


class UnknownToolError(LookupError):
    """A tool name that isn't in the flat namespace."""


class ConnectorUnavailableError(RuntimeError):
    """A connector (typically an MCP server) is down or never connected."""


@dataclass(frozen=True)
class Attachment:
    """One file the user sent with a chat message. The connector that owns
    the platform credentials downloads the bytes before emitting; the loop's
    attachment processor turns them into memory."""

    kind: str  # "image" | "audio" | "video" | "doc"
    filename: str
    media_type: str  # best-effort MIME type, "" when the platform doesn't say
    data: bytes

    @classmethod
    def from_record(cls, record: Any) -> Attachment:
        """A stored media record as one outbound file. The sniffed type
        decides the kind and the stored filename the name — the same
        direction-neutral shape the inbound path builds, so a stored
        blob can go back out without new plumbing. Duck-typed so the
        test fakes speak the same shape as MediaRecord."""
        filename = str(getattr(record, "filename", "") or "") or "file"
        media_type = str(getattr(record, "media_type", "") or "")
        return cls(
            classify_attachment(filename, media_type),
            filename,
            media_type,
            bytes(getattr(record, "data", b"")),
        )


_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic"})
_AUDIO_EXTENSIONS = frozenset({".mp3", ".ogg", ".oga", ".opus", ".m4a", ".wav", ".flac", ".aac"})
_VIDEO_EXTENSIONS = frozenset({".mp4", ".m4v", ".webm", ".mov", ".mkv", ".avi"})


def classify_attachment(filename: str, media_type: str = "") -> str:
    """ "image", "audio", or "video" when the platform or the filename says
    so, "doc" otherwise."""
    mime = media_type or ""
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    name = (filename or "").lower()
    if any(name.endswith(ext) for ext in _IMAGE_EXTENSIONS):
        return "image"
    if any(name.endswith(ext) for ext in _AUDIO_EXTENSIONS):
        return "audio"
    if any(name.endswith(ext) for ext in _VIDEO_EXTENSIONS):
        return "video"
    return "doc"


def split_message(text: str, limit: int) -> list[str]:
    """Text cut into platform-sized messages on line boundaries — the full
    text of an ask can outgrow one message, and no line may be lost to the
    cutting. A single line longer than the limit is hard-cut; every
    character still arrives."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit  # no line boundary in reach — a hard cut, not a loss
        chunks.append(text[:cut].rstrip("\n"))
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


@dataclass(frozen=True)
class InboundMessage:
    """One message from the user on some surface.

    `handle` is the sender's handle as the platform wrote it — normalization
    and the contact allowlist happen at ingest, not here.

    `reply_to_id`/`reply_to_text` carry what the message answers, when the
    platform says so — the id is what links a later "why?" back to the trace
    of the message it's about.

    `attachments` are the files the message carried, bytes already
    downloaded by the connector. `message_id` is the platform's own id for
    this message, when it gives one — structured logs correlate a turn
    with the exact inbound that started it.
    """

    surface: str  # "telegram" | "discord" | "web"
    handle: str
    text: str
    chat_ref: str  # opaque, surface-specific reply target
    reply_to_id: str = ""  # platform id of the message this one answers, if any
    reply_to_text: str = ""  # its text, when the platform hands it over
    attachments: tuple[Attachment, ...] = ()
    message_id: str = ""  # this message's own platform id, when there is one


InboundHandler = Callable[[InboundMessage], Awaitable[None]]
# (approval_id, APPROVED | DENIED) — the Phase-6 executor's cue
DecisionCallback = Callable[[int, str], Awaitable[None]]
# (task_id, "approve" | "revise" | "discard") -> a short plain-words note
# when the tap didn't apply (stale, double-pressed), None when it did —
# the proactive worker's decide_task owns the freshness
TaskDecisionCallback = Callable[[int, str], Awaitable[str | None]]
# (tool name, params, why-it-parked note) -> the ask's complete plain text,
# the renderer behind a chat surface's "read the full text" button — wired
# only where a surface shows that button, so the rest never see it
FullAskRenderer = Callable[[str, Any, str], str]


class ApprovalDecider(Protocol):
    """What messaging connectors need from the approvals store."""

    async def decide(self, approval_id: int, decision: str) -> Any: ...
    async def get(self, approval_id: int) -> Any: ...


class MessagingConnector:
    """Shared approve/deny + inbound plumbing; SDK glue lives in subclasses.

    Every surface presents the same buttons: a pending approval goes out
    with Approve/Deny attached, the press lands in one place (`_decide`),
    and only a *fresh* decision (not an expired or double-pressed one)
    fires `on_decision`.
    """

    name = ""

    def __init__(
        self,
        *,
        enabled: bool,
        approvals: ApprovalDecider | None = None,
        on_decision: DecisionCallback | None = None,
        on_inbound: InboundHandler | None = None,
        on_task_decision: TaskDecisionCallback | None = None,
        render_full: FullAskRenderer | None = None,
    ) -> None:
        self._enabled = enabled
        self._approvals = approvals
        self._on_decision = on_decision
        self._on_inbound = on_inbound
        self._on_task_decision = on_task_decision
        self._render_full = render_full

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def start(self) -> None:
        """Lifecycle contract every surface shares; a surface with nothing to
        start up (or one stopped for config) overrides this."""

    async def stop(self) -> None:
        """The shutdown counterpart — a surface that never started is a
        no-op, so the fanout can stop everything unconditionally."""

    async def _decide(self, approval_id: int, decision: str) -> Any:
        """The one shared path behind every platform's approve/deny buttons."""
        result = None
        if self._approvals is not None:
            result = await self._approvals.decide(approval_id, decision)
        if result is not None and self._on_decision is not None:
            await self._on_decision(approval_id, decision)
        return result

    async def _decide_and_explain(self, approval_id: int, decision: str) -> str | None:
        """Runs the shared decide path; returns None on a fresh decision (no
        feedback needed — the model's reply is the acknowledgment), or
        a short, platform-neutral phrase when the tap didn't apply, so a
        stale or double tap is never met with silence."""
        if await self._decide(approval_id, decision) is not None:
            return None
        if self._approvals is None:
            return "that request could not be found."
        approval = await self._approvals.get(approval_id)
        if approval is None:
            return "that request could not be found."
        if approval.status in (PENDING, EXPIRED):
            # still PENDING but decide() couldn't apply it — the only way
            # that happens is the expiry clock running out between the
            # button rendering and this tap
            return "that approval has expired."
        return "that request was already decided."

    async def _read_full_text(self, approval_id: int) -> str | None:
        """The complete text behind a surface's read button, or None when
        the request is no longer stored — a stale read gets the same plain
        words a stale decide does. Reading never decides anything: the
        buttons on the ask stay live."""
        if self._approvals is None or self._render_full is None:
            return None
        approval = await self._approvals.get(approval_id)
        if approval is None:
            return None
        return self._render_full(approval.tool_name, approval.params, approval.note)

    async def _decide_task(self, task_id: int, decision: str) -> str | None:
        """The shared path behind a result's one-tap verdicts. The worker
        owns freshness — a second tap on an already-decided task answers
        plainly there. None means the tap applied; the verdict's outcome
        (the carried-out action's own words, or the button going quiet)
        is the acknowledgment."""
        if self._on_task_decision is None:
            return "that task could not be found."
        return await self._on_task_decision(task_id, decision)

    async def present_result(self, task_id: int, text: str, options: Sequence[str] = ()) -> None:
        """A finished background task's result, with the one-tap verdicts
        that apply to it. A surface that doesn't do buttons needs nothing
        here — the words still reach its chat through the result's plain
        text, and the decision rides any other surface or the panel."""

    async def _emit_inbound(self, message: InboundMessage) -> None:
        if self._on_inbound is not None:
            await self._on_inbound(message)
