"""Telegram surface: long polling (no public URL needed), DMs in, replies
and one-tap approval buttons out.

Only private chats are processed — a personal agent, not a group bot.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import NetworkError
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

from ..authz.approvals import APPROVED, DENIED
from .base import (
    ApprovalDecider,
    DecisionCallback,
    InboundHandler,
    InboundMessage,
    MessagingConnector,
)

log = logging.getLogger("aether.connectors.telegram")

_CALLBACK_PREFIX = "aether:"
# A send opens a fresh TLS connection whenever the pooled one has gone stale
# (httpx keep-alive is ~5s) — on a throttled network that connect fails now
# and then, so sends get a few bounded tries before giving up on this
# surface; the fanout then logs and carries on to the others.
SEND_ATTEMPTS = 3
SEND_BACKOFF_SECONDS = 2.0


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
        app_factory: Any = None,
    ) -> None:
        super().__init__(
            enabled=enabled,
            approvals=approvals,
            on_decision=on_decision,
            on_inbound=on_inbound,
        )
        self._token = token
        self._app_factory = app_factory or self._default_app_factory
        self._app: Any = None
        self._chat_ref: str | None = None
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
        app.add_handler(
            MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, self._on_message)
        )
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
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None or not message.text:
            return
        handle = f"@{user.username}" if user.username else f"id:{user.id}"
        chat_ref = str(message.chat_id)
        self._chat_ref = chat_ref
        await self._emit_inbound(
            InboundMessage(surface="telegram", handle=handle, text=message.text, chat_ref=chat_ref)
        )

    async def _on_button(self, update: Update, context: Any) -> None:
        query = update.callback_query
        data = query.data if query is not None else ""
        if not data or not data.startswith(_CALLBACK_PREFIX):
            return
        await query.answer()
        action, _, id_part = data[len(_CALLBACK_PREFIX) :].partition(":")
        if not id_part.isdigit() or action not in ("approve", "deny"):
            log.warning("odd telegram callback data: %r", data)
            return
        decision = APPROVED if action == "approve" else DENIED
        result = await self._decide(int(id_part), decision)
        note = "handled." if result is not None else "already decided or expired."
        with contextlib.suppress(
            Exception
        ):  # message too old to edit — non-fatal, the store has the truth
            await query.edit_message_text(f"Approval {decision}: {note}", reply_markup=None)

    async def _on_error(self, update: object, context: Any) -> None:
        """One clean line — without a registered handler PTB dumps the whole
        'No error handlers are registered' traceback per failed update."""
        log.error("telegram update failed: %s", getattr(context, "error", "unknown error"))

    # -- outbound --------------------------------------------------------------------

    async def send_to_user(self, text: str) -> None:
        await self._send(text, reply_markup=None)

    async def present_approval(self, approval_id: int, tool_name: str, summary: str) -> None:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Approve ✅", callback_data=f"{_CALLBACK_PREFIX}approve:{approval_id}"
                    ),
                    InlineKeyboardButton(
                        "Deny ❌", callback_data=f"{_CALLBACK_PREFIX}deny:{approval_id}"
                    ),
                ]
            ]
        )
        await self._send(f"Approval needed: {tool_name}\n{summary}", reply_markup=keyboard)

    async def _send(self, text: str, reply_markup: Any) -> None:
        app = self._app
        if app is None or self._chat_ref is None:
            log.info("telegram: nowhere to send yet (started=%s)", app is not None)
            return
        for attempt in range(1, SEND_ATTEMPTS + 1):
            try:
                await app.bot.send_message(
                    chat_id=int(self._chat_ref), text=text, reply_markup=reply_markup
                )
                return
            except NetworkError:
                if attempt == SEND_ATTEMPTS:
                    raise
                log.warning(
                    "telegram send failed (attempt %d/%d) — retrying", attempt, SEND_ATTEMPTS
                )
                await asyncio.sleep(self._send_backoff * attempt)
