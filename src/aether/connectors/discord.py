"""Discord surface: gateway over WebSocket (no public URL), DMs in, replies
and approval buttons out. DMs only — the bot ignores servers and channels.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
from collections.abc import Sequence
from typing import Any

import discord

from ..authz.approvals import APPROVED, DENIED
from .attachments import MAX_ATTACHMENT_BYTES, actual_size_exceeds_cap, reported_size_exceeds_cap
from .base import (
    TASK_VERDICT_LABELS,
    ApprovalDecider,
    Attachment,
    DecisionCallback,
    FullAskRenderer,
    InboundHandler,
    InboundMessage,
    MessagingConnector,
    TaskDecisionCallback,
    classify_attachment,
    split_message,
)

log = logging.getLogger("aether.connectors.discord")

# Discord caps bots at 10 MB per upload — a bigger file is refused
# honestly rather than bouncing off the API
UPLOAD_CAP_BYTES = 10 * 1024 * 1024
# Discord caps one message at 2000 characters — the full text of an ask
# can outgrow that, so the read button sends it in whole lines rather
# than letting the platform cut it
MESSAGE_CAP_CHARS = 2000


class ApprovalView(discord.ui.View):
    """In-memory approve/deny buttons; they live exactly as long as the bot
    process. Approvals that outlive a restart surface in the web panel.

    With a read callback wired, a third button shows the whole ask — a
    read never retires the decision buttons, so looking closer never
    costs the one-tap path."""

    def __init__(self, decide_and_explain: Any, approval_id: int, read_full: Any = None) -> None:
        super().__init__(timeout=None)
        self._decide_and_explain = decide_and_explain
        self._approval_id = approval_id
        if read_full is not None:
            # built at runtime like the result view's verdict buttons — the
            # button exists only where the full-text renderer is wired
            button = discord.ui.Button(
                label="Read the full text", style=discord.ButtonStyle.secondary, row=1
            )
            button.callback = self._read_tap(read_full)
            self.add_item(button)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.green)
    async def approve(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        await self._finish(interaction, APPROVED)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.red)
    async def deny(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        await self._finish(interaction, DENIED)

    def _read_tap(self, read_full: Any) -> Any:
        async def tap(interaction: discord.Interaction) -> None:
            text = await read_full(self._approval_id)
            if text is None:
                # the same plain words a stale decide tap gets — a tap that
                # applies to nothing must never land silently
                await interaction.response.send_message("that request could not be found.")
                return
            # the first chunk is the interaction's own reply; the rest
            # follow, whole lines at a time, so a long ask loses nothing
            chunks = split_message(text, MESSAGE_CAP_CHARS)
            await interaction.response.send_message(content=chunks[0])
            for chunk in chunks[1:]:
                await interaction.followup.send(content=chunk)

        return tap

    async def _finish(self, interaction: Any, decision: str) -> None:
        note = await self._decide_and_explain(self._approval_id, decision)
        for child in self.children:
            child.disabled = True
        # a fresh decision needs no extra note — `content` is left out
        # entirely so the question text stays untouched (py-cord treats an
        # explicit content=None as "clear it", not "leave it alone"); a
        # stale or double tap gets a short note instead of landing with no
        # feedback at all
        if note:
            await interaction.response.edit_message(content=note, view=self)
        else:
            await interaction.response.edit_message(view=self)


class ResultView(discord.ui.View):
    """A background task's one-tap verdicts, built from the options the
    task actually offers — in-memory like the approval buttons, so a
    restart's tasks surface in the web panel instead."""

    _LABELS = TASK_VERDICT_LABELS
    _STYLES = {
        "approve": discord.ButtonStyle.green,
        "revise": discord.ButtonStyle.secondary,
        "discard": discord.ButtonStyle.red,
    }

    def __init__(
        self,
        decide_task: Any,
        task_id: int,
        options: Sequence[str] = (),
    ) -> None:
        super().__init__(timeout=None)
        self._decide_task = decide_task
        self._task_id = task_id
        for verb, label in self._LABELS.items():
            if verb not in options:
                continue
            button = discord.ui.Button(label=label, style=self._STYLES[verb])
            button.callback = self._pressed(verb)
            self.add_item(button)

    def _pressed(self, decision: str) -> Any:
        async def press(interaction: discord.Interaction) -> None:
            note = await self._decide_task(self._task_id, decision)
            for child in self.children:
                # py-cord's stubs leave `disabled` off Item — the attribute
                # exists on every real button, so the call is dynamic
                setattr(child, "disabled", True)  # noqa: B010 — see comment above
            # same trade as the approval buttons: a fresh verdict needs no
            # extra note (the outcome is the acknowledgment); a stale or
            # double tap gets a short one so it never lands silently
            if note:
                await interaction.response.edit_message(content=note, view=self)
            else:
                await interaction.response.edit_message(view=self)

        return press


def _is_dm(channel: Any) -> bool:
    """Duck-typed so unit tests can pass lightweight fakes."""
    return getattr(channel, "type", None) == discord.ChannelType.private


class DiscordConnector(MessagingConnector):
    name = "discord"

    def __init__(
        self,
        *,
        token: str,
        enabled: bool = True,
        approvals: ApprovalDecider | None = None,
        on_decision: DecisionCallback | None = None,
        on_inbound: InboundHandler | None = None,
        on_task_decision: TaskDecisionCallback | None = None,
        render_full: FullAskRenderer | None = None,
        bot_factory: Any = None,
    ) -> None:
        super().__init__(
            enabled=enabled,
            approvals=approvals,
            on_decision=on_decision,
            on_inbound=on_inbound,
            on_task_decision=on_task_decision,
            render_full=render_full,
        )
        self._token = token
        self._bot_factory = bot_factory or self._default_bot_factory
        self._bot: Any = None
        self._task: asyncio.Task[None] | None = None
        self._channel: Any = None

    @staticmethod
    def _default_bot_factory() -> Any:
        # message content is a privileged intent — the user must flip it on
        # in the Discord developer portal too (noted in the README config table)
        intents = discord.Intents.default()
        intents.message_content = True
        return discord.Bot(intents=intents)

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        if not self._enabled or not self._token:
            log.info(
                "discord connector not started (enabled=%s, token set=%s)",
                self._enabled,
                bool(self._token),
            )
            return
        bot = self._bot_factory()
        bot.event(self.on_message)
        self._bot = bot
        self._task = asyncio.create_task(self._run_bot(), name="discord-gateway")
        log.info("discord connector starting")

    async def _run_bot(self) -> None:
        try:
            await self._bot.start(self._token)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "discord gateway stopped — check DISCORD_BOT_TOKEN and the privileged intents"
            )

    async def stop(self) -> None:
        bot, self._bot = self._bot, None
        if bot is not None:
            try:
                await bot.close()
            except Exception:
                log.exception("discord close failed")
        task, self._task = self._task, None
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # -- inbound -----------------------------------------------------------------

    async def on_message(self, message: Any) -> None:
        if message.author.bot or not _is_dm(message.channel):
            return
        attachments = await self._collect_attachments(message)
        if not message.content and not attachments:
            return
        self._channel = message.channel
        handle = message.author.name or str(message.author.id)
        reference = getattr(message, "reference", None)
        await self._emit_inbound(
            InboundMessage(
                surface="discord",
                handle=handle,
                text=message.content,
                chat_ref=str(message.author.id),
                # fetching the answered text costs an extra API round-trip —
                # the id alone links a later "why?" back to its trace
                reply_to_id=(
                    str(getattr(reference, "message_id", None) or "")
                    if reference is not None
                    else ""
                ),
                attachments=tuple(attachments),
            )
        )

    async def _collect_attachments(self, message: Any) -> list[Attachment]:
        """Files the user sent, downloaded now — the connector holds the
        platform credentials. A file that can't be downloaded (oversize,
        network) is logged and skipped; it must never take the surface down."""
        out: list[Attachment] = []
        for attachment in list(getattr(message, "attachments", None) or []):
            name = getattr(attachment, "filename", None) or "file"
            # the log lines below never carry the filename — a file the
            # user sent is sensitive data, and "which file was too big" is
            # a count, never a name
            if reported_size_exceeds_cap(getattr(attachment, "size", None), MAX_ATTACHMENT_BYTES):
                log.warning("discord file over the size cap — skipped")
                continue
            try:
                data = await attachment.read()
            except Exception:
                log.warning("discord file download failed — continuing", exc_info=True)
                continue
            # the one check that can't be lied to: whatever the reported
            # size claimed, a file that's actually too big is never kept
            if actual_size_exceeds_cap(data, MAX_ATTACHMENT_BYTES):
                log.warning(
                    "discord file exceeded the size cap after download (%d bytes) — discarded",
                    len(data),
                )
                continue
            media = getattr(attachment, "content_type", None) or ""
            out.append(Attachment(classify_attachment(name, media), name, media, data))
        return out

    # -- outbound ------------------------------------------------------------------

    async def send_to_user(self, text: str, *, attachment: Attachment | None = None) -> str | None:
        """Send, and answer with the platform message id — what links a
        later "why?" reply back to the trace of this send."""
        if self._channel is None:
            log.info("discord: no DM channel yet")
            return None
        sent = await self._send(text, None, attachment)
        # getattr so test fakes without an id still send fine
        return str(getattr(sent, "id", "") or "") or None

    async def present_approval(
        self, approval_id: int, text: str, *, attachment: Attachment | None = None
    ) -> None:
        """One plain-language question plus one-tap Approve/Deny. The raw
        tool name never reaches the user — the decision record keeps it."""
        if self._channel is None:
            log.info("discord: no DM channel yet — approval visible in the web panel")
            return
        await self._send(
            text,
            ApprovalView(
                self._decide_and_explain,
                approval_id,
                read_full=self._read_full_text if self._render_full is not None else None,
            ),
            attachment,
        )

    async def present_result(
        self,
        task_id: int,
        text: str,
        options: Sequence[str] = (),
        *,
        attachment: Attachment | None = None,
    ) -> None:
        """A finished background task's result plus the one-tap verdicts
        that apply to it."""
        if self._channel is None:
            log.info("discord: no DM channel yet — result visible in the web panel")
            return
        await self._send(text, ResultView(self._decide_task, task_id, options), attachment)

    async def _send(self, text: str, view: Any, attachment: Attachment | None) -> Any:
        """One message out — the words plus a fresh single-use File when
        media rides along (a discord.File is consumed by the send, so
        every send builds its own). A file Discord cannot take is refused
        honestly: the words still land, with a plain note in place of
        the file."""
        if attachment is None:
            return await self._channel.send(content=text, view=view)
        if len(attachment.data) > UPLOAD_CAP_BYTES:
            log.info(
                "discord: attachment over the upload cap (%d bytes) — not sent",
                len(attachment.data),
            )
            return await self._channel.send(
                content=f"{text}\n\n(the file is too big for Discord and was not sent)",
                view=view,
            )
        file = discord.File(io.BytesIO(attachment.data), filename=attachment.filename or "file")
        return await self._channel.send(content=text, view=view, file=file)
