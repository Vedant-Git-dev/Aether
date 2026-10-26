"""Telegram surface: long polling (no public URL needed), DMs in, replies
and one-tap approval buttons out.

Only private chats are processed — a personal agent, not a group bot.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters, Update
from telegram.error import Conflict, NetworkError
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

from ..authz.approvals import APPROVED, DENIED
from .attachments import MAX_ATTACHMENT_BYTES, actual_size_exceeds_cap, reported_size_exceeds_cap
from .base import (
    ApprovalDecider,
    Attachment,
    DecisionCallback,
    InboundHandler,
    InboundMessage,
    MessagingConnector,
    TaskDecisionCallback,
    classify_attachment,
)
from .processed import ProcessedUpdates

log = logging.getLogger("aether.connectors.telegram")

_CALLBACK_PREFIX = "aether:"
# A send opens a fresh TLS connection whenever the pooled one has gone stale
# (httpx keep-alive is ~5s) — on a throttled network that connect fails now
# and then, so sends get a few bounded tries before giving up on this
# surface; the fanout then logs and carries on to the others.
SEND_ATTEMPTS = 3
SEND_BACKOFF_SECONDS = 2.0
# Telegram caps photos at 10 MB and documents at 50 MB — a bigger image
# falls back to a document rather than failing the send, and a bigger
# document is refused honestly instead of bouncing off the API
PHOTO_CAP_BYTES = 10 * 1024 * 1024
DOCUMENT_CAP_BYTES = 50 * 1024 * 1024
# Telegram caps captions at 1024 characters — a text that doesn't fit
# rides as its own message after the file, so no word is silently cut
CAPTION_CAP_CHARS = 1000


class TelegramConnector(MessagingConnector):
    name = "telegram"

    def __init__(
        self,
        *,
        token: str,
        enabled: bool = True,
        approvals: ApprovalDecider | None = None,
        on_decision: DecisionCallback | None = None,
        on_inbound: InboundHandler | None = None,
        on_task_decision: TaskDecisionCallback | None = None,
        processed: ProcessedUpdates | None = None,
        app_factory: Any = None,
    ) -> None:
        super().__init__(
            enabled=enabled,
            approvals=approvals,
            on_decision=on_decision,
            on_inbound=on_inbound,
            on_task_decision=on_task_decision,
        )
        self._token = token
        self._processed = processed
        self._app_factory = app_factory or self._default_app_factory
        self._app: Any = None
        self._chat_ref: str | None = None
        self._reply_message_id: int | None = None
        self._send_backoff = SEND_BACKOFF_SECONDS

    @staticmethod
    def _default_app_factory(token: str) -> Any:
        return Application.builder().token(token).build()

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        if not self._enabled or not self._token:
            log.info(
                "telegram connector not started (enabled=%s, token set=%s)",
                self._enabled,
                bool(self._token),
            )
            return
        app = self._app_factory(self._token)
        # commands ride the private-chat filter too: the loop owns the
        # deterministic vocabulary (/verify) and anything it doesn't handle
        # flows to the model like any other message — dropping /commands
        # here only made Aether look deaf
        app.add_handler(MessageHandler(filters.ChatType.PRIVATE, self._on_message))
        app.add_handler(CallbackQueryHandler(self._on_button))
        app.add_error_handler(self._on_error)
        await app.initialize()
        await app.start()
        if app.updater is not None:
            await app.updater.start_polling()
        self._app = app
        log.info("telegram connector started")

    async def stop(self) -> None:
        app, self._app = self._app, None
        if app is None:
            return
        if app.updater is not None:
            await app.updater.stop()
        await app.stop()
        await app.shutdown()

    # -- inbound ------------------------------------------------------------------

    async def _on_message(self, update: Update, context: Any) -> None:
        # a retry or a restart re-delivers the same update — the durable
        # ledger decides "first sighting", never the in-memory offset
        if not await self._first_sighting(update):
            return
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None:
            return
        attachments = await self._collect_attachments(message)
        # the user's words live in the caption when the message carries a file
        text = message.text or getattr(message, "caption", None) or ""
        if not text and not attachments:
            return
        handle = f"@{user.username}" if user.username else f"id:{user.id}"
        chat_ref = str(message.chat_id)
        self._chat_ref = chat_ref
        # the platform's own id for this message — outbound replies quote
        # it, so an answer is visually bound to what it answers, and the
        # structured log can correlate the two
        message_id = getattr(message, "message_id", None)
        self._reply_message_id = message_id
        log.info(
            "telegram inbound: chat=%s message=%s attachments=%d",
            chat_ref,
            message_id,
            len(attachments),
        )
        reply_to = getattr(message, "reply_to_message", None)
        reply_to_text = ""
        if reply_to is not None:
            # the answered message's words live in its caption when it
            # carried a file, and its own filename rides along — so
            # "send it back" as a reply resolves by metadata instead of
            # a guessed media id
            reply_to_text = reply_to.text or getattr(reply_to, "caption", None) or ""
            name = self._replied_file_name(reply_to)
            if name is not None:
                marker = f"(a message that carried the file '{name}')"
                reply_to_text = f"{reply_to_text} {marker}".strip()
        await self._emit_inbound(
            InboundMessage(
                surface="telegram",
                handle=handle,
                text=text,
                chat_ref=chat_ref,
                reply_to_id=str(reply_to.message_id) if reply_to is not None else "",
                reply_to_text=reply_to_text,
                attachments=tuple(attachments),
                message_id=str(message_id) if message_id is not None else "",
            )
        )

    async def _first_sighting(self, update: Any) -> bool:
        """True when this update was never processed before. The ledger is
        the durable word on delivery — but a ledger failure is never a
        reason to lose a real message, so it reads as a first sighting."""
        if self._processed is None:
            return True
        update_id = getattr(update, "update_id", None)
        if update_id is None:
            return True
        try:
            return await self._processed.record("telegram", int(update_id))
        except Exception:
            log.warning("telegram dedup ledger failed — processing anyway", exc_info=True)
            return True

    @staticmethod
    def _replied_file_name(message: Any) -> str | None:
        """The name of the file the replied-to message carried, from the
        platform's own fields — photos and voice notes have none, so
        they fall back to the same names the shared event uses."""
        document = getattr(message, "document", None)
        if document is not None:
            return getattr(document, "file_name", None) or "file"
        if getattr(message, "photo", None):
            return "photo.jpg"
        for field, fallback in (
            ("video", "video.mp4"),
            ("video_note", "video_note.mp4"),
            ("voice", "voice.ogg"),
        ):
            if getattr(message, field, None) is not None:
                return fallback
        audio = getattr(message, "audio", None)
        if audio is not None:
            return getattr(audio, "file_name", None) or "audio.mp3"
        return None

    async def _collect_attachments(self, message: Any) -> list[Attachment]:
        """Photos and files the user sent, downloaded now — the connector is
        the one holding the platform credentials. A file that can't be
        downloaded (oversize, network) is logged and skipped; it must never
        take the surface down."""
        out: list[Attachment] = []
        photos = list(getattr(message, "photo", None) or [])
        if photos:  # sizes ascending — the last is the largest
            data = await self._download(photos[-1])
            if data is not None:
                out.append(Attachment("image", "photo.jpg", "image/jpeg", data))
        document = getattr(message, "document", None)
        if document is not None:
            name = getattr(document, "file_name", None) or "file"
            media = getattr(document, "mime_type", None) or ""
            data = await self._download(document)
            if data is not None:
                out.append(Attachment(classify_attachment(name, media), name, media, data))
        # voice notes and video notes have their own fields — without these
        # they arrived as empty messages and were silently dropped
        voice = getattr(message, "voice", None)
        if voice is not None:
            data = await self._download(voice)
            if data is not None:
                out.append(Attachment("audio", "voice.ogg", "audio/ogg", data))
        video_note = getattr(message, "video_note", None)
        if video_note is not None:
            data = await self._download(video_note)
            if data is not None:
                out.append(Attachment("video", "video_note.mp4", "video/mp4", data))
        # a video sent as a video (not a note) and a music file ride their
        # own fields too — without these the bytes never arrived at all and
        # a captioned video was answered text-only, as if unsent
        video = getattr(message, "video", None)
        if video is not None:
            data = await self._download(video)
            if data is not None:
                out.append(Attachment("video", "video.mp4", "video/mp4", data))
        audio = getattr(message, "audio", None)
        if audio is not None:
            name = getattr(audio, "file_name", None) or "audio.mp3"
            media = getattr(audio, "mime_type", None) or "audio/mpeg"
            data = await self._download(audio)
            if data is not None:
                out.append(Attachment("audio", name, media, data))
        return out

    async def _download(self, telegram_file: Any) -> bytes | None:
        # the log lines below never carry the filename — a file the user
        # sent is sensitive data, and "which file was too big" is a count,
        # never a name
        if reported_size_exceeds_cap(
            getattr(telegram_file, "file_size", None), MAX_ATTACHMENT_BYTES
        ):
            log.warning("telegram file over the size cap — skipped")
            return None
        try:
            file = await telegram_file.get_file()
            # getFile's own file_size is Telegram's more authoritative word
            # on the file now that a file_path exists — still just a hint
            # (also Optional per the Bot API), never trusted on its own
            if reported_size_exceeds_cap(getattr(file, "file_size", None), MAX_ATTACHMENT_BYTES):
                log.warning("telegram file over the size cap — skipped")
                return None
            data = bytes(await file.download_as_bytearray())
        except Exception:
            log.warning("telegram file couldn't be downloaded — continuing", exc_info=True)
            return None
        # the one check that can't be lied to: whatever either size claimed,
        # a file that's actually too big is never kept in memory
        if actual_size_exceeds_cap(data, MAX_ATTACHMENT_BYTES):
            log.warning(
                "telegram file exceeded the size cap after download (%d bytes) — discarded",
                len(data),
            )
            return None
        return data

    async def _on_button(self, update: Update, context: Any) -> None:
        query = update.callback_query
        if query is None:
            return
        data = query.data or ""
        if not data or not data.startswith(_CALLBACK_PREFIX):
            return
        # the outcome belongs in the chat where the tap happened, not
        # wherever the last inbound arrived — and it quotes the card it
        # answers, so it reads as the answer to the tapped question
        tapped = getattr(query, "message", None)
        tapped_chat = getattr(tapped, "chat", None) if tapped is not None else None
        tapped_chat_id = getattr(tapped_chat, "id", None)
        if tapped_chat_id is not None:
            self._chat_ref = str(tapped_chat_id)
            self._reply_message_id = getattr(tapped, "message_id", None)
        body = data[len(_CALLBACK_PREFIX) :]
        # a task verdict rides its own namespace — "task:<verb>:<task id>"
        # — so it can never collide with an approval's "<verb>:<approval id>"
        if body.startswith("task:"):
            verb, _, id_part = body[len("task:") :].partition(":")
            if verb not in ("approve", "revise", "discard") or not id_part.isdigit():
                log.warning("odd telegram callback data: %r", data)
                with contextlib.suppress(Exception):
                    await query.answer()
                return
            note = await self._decide_task(int(id_part), verb)
            with contextlib.suppress(Exception):  # answered whatever happened
                await query.answer(text=note) if note else await query.answer()
            with contextlib.suppress(Exception):
                await query.edit_message_reply_markup(reply_markup=None)
            return
        action, _, id_part = body.partition(":")
        if not id_part.isdigit() or action not in ("approve", "deny"):
            log.warning("odd telegram callback data: %r", data)
            with contextlib.suppress(Exception):
                await query.answer()
            return
        decision = APPROVED if action == "approve" else DENIED
        note = await self._decide_and_explain(int(id_part), decision)
        with contextlib.suppress(Exception):  # the callback is still answered either way
            # a fresh decision needs no toast — the outcome message from
            # _carry_out is the acknowledgment; a stale or double tap gets
            # a quick toast instead of landing with no feedback at all
            await query.answer(text=note) if note else await query.answer()
        with contextlib.suppress(
            Exception
        ):  # message too old to edit — non-fatal, the store has the truth
            await query.edit_message_reply_markup(reply_markup=None)

    async def _on_error(self, update: object, context: Any) -> None:
        """One clean line — without a registered handler PTB dumps the whole
        'No error handlers are registered' traceback per failed update."""
        error = getattr(context, "error", None)
        if isinstance(error, Conflict):
            # Telegram allows one getUpdates per bot token: a 409 means
            # another process is polling with the same token (a second
            # instance, or a leftover webhook). Saying so plainly beats a
            # stream of identical errors with no diagnosis attached.
            log.error(
                "telegram: another poller is using this bot token — Telegram "
                "allows one getUpdates at a time, so this instance keeps "
                "losing the race; stop the other process and it recovers"
            )
            return
        log.error("telegram update failed: %s", error or "unknown error")

    # -- outbound --------------------------------------------------------------------

    async def show_typing(self) -> None:
        """Telegram's own "typing…" marker — the surface saying work is
        happening instead of silence. Cosmetic: nothing here may fail the
        turn, and no chat known yet is a no-op, not an error."""
        app = self._app
        if app is None or self._chat_ref is None:
            return
        with contextlib.suppress(Exception):
            await app.bot.send_chat_action(chat_id=int(self._chat_ref), action="typing")

    async def send_to_user(self, text: str, *, attachment: Attachment | None = None) -> str | None:
        """Send, and answer with the platform message id — what links a
        later "why?" reply back to the trace of this send."""
        return await self._send(text, reply_markup=None, attachment=attachment)

    async def present_approval(
        self, approval_id: int, text: str, *, attachment: Attachment | None = None
    ) -> None:
        """One plain-language question plus one-tap Approve/Deny. The raw
        tool name never reaches the user — the decision record keeps it."""
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Approve", callback_data=f"{_CALLBACK_PREFIX}approve:{approval_id}"
                    ),
                    InlineKeyboardButton(
                        "Deny", callback_data=f"{_CALLBACK_PREFIX}deny:{approval_id}"
                    ),
                ]
            ]
        )
        await self._send(text, reply_markup=keyboard, attachment=attachment)

    async def present_result(
        self,
        task_id: int,
        text: str,
        options: Sequence[str] = (),
        *,
        attachment: Attachment | None = None,
    ) -> None:
        """A finished background task's result plus the one-tap verdicts
        that apply to it. An odd verdict name never reaches the buttons —
        the fixed vocabulary stays fixed."""
        labels = {"approve": "Approve", "revise": "Revise", "discard": "Discard"}
        buttons = [
            InlineKeyboardButton(
                labels[verb], callback_data=f"{_CALLBACK_PREFIX}task:{verb}:{task_id}"
            )
            for verb in ("approve", "revise", "discard")
            if verb in options
        ]
        keyboard = InlineKeyboardMarkup([buttons]) if buttons else None
        await self._send(text, reply_markup=keyboard, attachment=attachment)

    async def _send(
        self, text: str, reply_markup: Any, attachment: Attachment | None = None
    ) -> str | None:
        app = self._app
        if app is None or self._chat_ref is None:
            log.info("telegram: nowhere to send yet (started=%s)", app is not None)
            return None
        for attempt in range(1, SEND_ATTEMPTS + 1):
            try:
                kwargs: dict[str, Any] = {}
                if self._reply_message_id is not None:
                    # every reply visually quotes the message it answers, so
                    # a busy chat reads in order instead of guessing which
                    # question an answer belongs to. The target can be gone
                    # (deleted, or a proactive send long after the last
                    # inbound) — then Telegram sends without the quote
                    # rather than failing the reply
                    kwargs["reply_parameters"] = ReplyParameters(
                        message_id=self._reply_message_id,
                        allow_sending_without_reply=True,
                    )
                if attachment is None:
                    sent = await app.bot.send_message(
                        chat_id=int(self._chat_ref), text=text, reply_markup=reply_markup, **kwargs
                    )
                else:
                    sent = await self._send_file(app, text, reply_markup, attachment, kwargs)
                # getattr so test fakes without a message_id still send fine
                return str(getattr(sent, "message_id", "") or "") or None
            except NetworkError:
                if attempt == SEND_ATTEMPTS:
                    raise
                log.warning(
                    "telegram send failed (attempt %d/%d) — retrying", attempt, SEND_ATTEMPTS
                )
                await asyncio.sleep(self._send_backoff * attempt)

    async def _send_file(
        self, app: Any, text: str, reply_markup: Any, attachment: Attachment, kwargs: dict[str, Any]
    ) -> Any:
        """One file out through Telegram. Small images ride as photos,
        everything else as documents — both keep the buttons, and the
        filename is passed so the stored name survives the trip. A file
        Telegram cannot take at all is refused honestly: the words still
        land, with a plain note in place of the file. A text too long
        for a caption follows as its own message instead of being cut."""
        chat_id = int(self._chat_ref)
        size = len(attachment.data)
        if size > DOCUMENT_CAP_BYTES:
            log.info("telegram: attachment over the document cap (%d bytes) — not sent", size)
            return await app.bot.send_message(
                chat_id=chat_id,
                text=f"{text}\n\n(the file is too big for Telegram and was not sent)",
                reply_markup=reply_markup,
                **kwargs,
            )
        caption = text if len(text) <= CAPTION_CAP_CHARS else None
        if attachment.kind == "image" and size <= PHOTO_CAP_BYTES:
            sent = await app.bot.send_photo(
                chat_id=chat_id,
                photo=attachment.data,
                caption=caption,
                reply_markup=reply_markup,
                **kwargs,
            )
        else:
            sent = await app.bot.send_document(
                chat_id=chat_id,
                document=attachment.data,
                filename=attachment.filename or "file",
                caption=caption,
                reply_markup=reply_markup,
                **kwargs,
            )
        if caption is None and text:
            # the words didn't fit the caption — they follow as their own
            # message, carrying the buttons so the one-tap path stays
            return await app.bot.send_message(
                chat_id=chat_id, text=text, reply_markup=reply_markup, **kwargs
            )
        return sent
