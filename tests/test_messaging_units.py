"""Messaging connector tests — all with fakes; no network, no SDK clients.

The platform SDK glue (handler registration, polling, sockets) is exercised
against injected fakes here, and live-verified manually during deployment
checks. The decide/inbound plumbing shared by every surface is tested via
the MessagingConnector base class.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import discord
import httpx
import pytest
from fakes import FakeApprovals, FakeSurfaceConnector
from telegram import Chat, Message, Update
from telegram.error import Conflict, NetworkError

from aether.agent.loop import SurfaceFanout
from aether.authz.approvals import APPROVED, DENIED
from aether.config import AppConfig, MessagingConfig, PlatformToggle, Settings
from aether.connectors import build_messaging_connectors
from aether.connectors.base import InboundMessage, MessagingConnector
from aether.connectors.discord import ApprovalView, DiscordConnector
from aether.connectors.slack import (
    APPROVE_ACTION,
    DENY_ACTION,
    TASK_ACTION,
    SlackConnector,
    approval_blocks,
    event_to_inbound,
    result_blocks,
)
from aether.connectors.telegram import CAPTION_CAP_CHARS, SEND_ATTEMPTS, TelegramConnector

# ---------------------------------------------------------------------------
# shared decide/inbound plumbing (base class)
# ---------------------------------------------------------------------------


async def test_fresh_decisions_fire_on_decision_stale_do_not() -> None:
    approvals = FakeApprovals()
    decisions: list[tuple[int, str]] = []

    async def on_decision(approval_id: int, decision: str) -> None:
        decisions.append((approval_id, decision))

    connector = MessagingConnector(
        enabled=True, approvals=approvals, on_decision=on_decision, on_inbound=None
    )
    fresh = await connector._decide(5, APPROVED)
    assert fresh is not None and fresh.status == APPROVED
    assert approvals.calls == [(5, APPROVED)]
    assert decisions == [(5, APPROVED)]

    # a stale/double press returns None and must not re-fire the executor
    approvals.result = None
    assert await connector._decide(5, DENIED) is None
    assert approvals.calls[-1] == (5, DENIED)
    assert decisions == [(5, APPROVED)]


async def test_task_verdicts_pass_through_and_say_so_without_a_callback() -> None:
    verdicts: list[tuple[int, str]] = []

    async def on_task_decision(task_id: int, verb: str) -> str | None:
        verdicts.append((task_id, verb))
        return "dropped — I'll do less of that."

    wired = MessagingConnector(enabled=True, on_task_decision=on_task_decision, on_inbound=None)
    assert await wired._decide_task(3, "discard") == "dropped — I'll do less of that."
    assert verdicts == [(3, "discard")]

    # no callback (the worker absent, a bare connector) — never silence
    bare = MessagingConnector(enabled=True)
    assert await bare._decide_task(3, "approve") == "that task could not be found."


# ---------------------------------------------------------------------------
# connector factory: enabled flag AND tokens
# ---------------------------------------------------------------------------


def _settings(**kwargs) -> Settings:
    return Settings.without_env_file(**kwargs)


def _config(telegram=False, discord_=False, slack=False) -> AppConfig:
    return AppConfig(
        messaging=MessagingConfig(
            telegram=PlatformToggle(enabled=telegram),
            discord=PlatformToggle(enabled=discord_),
            slack=PlatformToggle(enabled=slack),
        )
    )


def test_factory_skips_half_configured_surfaces() -> None:
    settings = _settings(telegram_bot_token="tg-token")  # discord/slack tokens missing
    built = build_messaging_connectors(settings, _config(telegram=True, discord_=True, slack=True))
    assert [c.name for c in built] == ["telegram"]


def test_factory_builds_each_surface_when_fully_configured() -> None:
    settings = _settings(
        telegram_bot_token="tg",
        discord_bot_token="dc",
        slack_bot_token="xoxb-",
        slack_app_token="xoxp-",
    )
    built = build_messaging_connectors(settings, _config(telegram=True, discord_=True, slack=True))
    assert [c.name for c in built] == ["telegram", "discord", "slack"]


def test_factory_with_everything_off_builds_nothing() -> None:
    settings = _settings(telegram_bot_token="tg")
    assert build_messaging_connectors(settings, _config()) == []


# ---------------------------------------------------------------------------
# telegram (fake Application)
# ---------------------------------------------------------------------------


class FakeTelegramBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(
        self, chat_id: int, text: str, reply_markup=None, **kwargs
    ) -> SimpleNamespace:
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup, **kwargs})
        # the way telegram answers a send — the id that links a why? back
        return SimpleNamespace(message_id=100 + len(self.sent))

    async def send_photo(
        self, chat_id: int, photo: bytes, caption=None, reply_markup=None, **kwargs
    ) -> SimpleNamespace:
        self.sent.append(
            {
                "chat_id": chat_id,
                "photo": photo,
                "caption": caption,
                "reply_markup": reply_markup,
                **kwargs,
            }
        )
        return SimpleNamespace(message_id=100 + len(self.sent))

    async def send_document(
        self,
        chat_id: int,
        document: bytes,
        filename=None,
        caption=None,
        reply_markup=None,
        **kwargs,
    ) -> SimpleNamespace:
        self.sent.append(
            {
                "chat_id": chat_id,
                "document": document,
                "filename": filename,
                "caption": caption,
                "reply_markup": reply_markup,
                **kwargs,
            }
        )
        return SimpleNamespace(message_id=100 + len(self.sent))


class FlakyTelegramBot(FakeTelegramBot):
    """Raises NetworkError on the first `failures` sends, then behaves — the
    shape of a throttled network dropping fresh TLS connects."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    async def send_message(
        self, chat_id: int, text: str, reply_markup=None, **kwargs
    ) -> SimpleNamespace:
        if self.failures > 0:
            self.failures -= 1
            raise NetworkError("httpx.ConnectError: tls handshake failed")
        return await super().send_message(chat_id, text, reply_markup, **kwargs)


class FakeTelegramApp:
    def __init__(self) -> None:
        # Any, not object: the handlers are PTB shapes the fakes never build,
        # and every test reaches through to .callback / .filters
        self.handlers: list[Any] = []
        self.error_handlers: list[Any] = []
        self.bot = FakeTelegramBot()
        self.updater = SimpleNamespace(start_polling=self._noop, stop=self._noop, polling=False)
        self.initialized = False
        self.started = False
        self.stopped = False
        self.shutdown = False

    async def _noop(self) -> None:
        pass

    def add_handler(self, handler) -> None:
        self.handlers.append(handler)

    def add_error_handler(self, handler) -> None:
        self.error_handlers.append(handler)

    async def initialize(self) -> None:
        self.initialized = True

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def shutdown(self) -> None:
        self.shutdown = True


class FakeProcessedLedger:
    """The in-memory double of ProcessedUpdates — a set is the whole
    contract: the first sighting wins, every later delivery is a dup."""

    def __init__(self, fail: bool = False) -> None:
        self.seen: set[tuple[str, int]] = set()
        self.fail = fail

    async def record(self, source: str, update_id: int) -> bool:
        if self.fail:
            raise RuntimeError("ledger down")
        key = (source, update_id)
        if key in self.seen:
            return False
        self.seen.add(key)
        return True


def _tg_update(
    text: str,
    username: str = "vedant",
    user_id: int = 1,
    chat_id: int = 42,
    reply_to=None,
    update_id: int | None = None,
):
    return SimpleNamespace(
        update_id=update_id,
        effective_message=SimpleNamespace(text=text, chat_id=chat_id, reply_to_message=reply_to),
        effective_user=SimpleNamespace(username=username, id=user_id),
    )


async def _started_telegram(
    approvals=None, decisions=None, inbound=None, task_decisions=None, processed=None
):
    app = FakeTelegramApp()
    factory_calls: list[str] = []

    def factory(token: str):
        factory_calls.append(token)
        return app

    connector = TelegramConnector(
        token="tg-token",
        approvals=approvals,
        on_decision=decisions,
        on_inbound=inbound,
        on_task_decision=task_decisions,
        processed=processed,
        app_factory=factory,
    )
    await connector.start()
    assert factory_calls == ["tg-token"]
    assert app.initialized and app.started and len(app.handlers) == 2
    assert len(app.error_handlers) == 1  # update failures log, never dump a traceback
    return connector, app


async def test_telegram_start_registers_message_and_button_handlers() -> None:
    _, app = await _started_telegram()
    kinds = {type(h).__name__ for h in app.handlers}
    assert kinds == {"MessageHandler", "CallbackQueryHandler"}
    # stop is clean and idempotent
    await TelegramConnector(token="", enabled=False, app_factory=None).stop()


async def test_telegram_inbound_dm_becomes_an_inbound_message() -> None:
    received: list[InboundMessage] = []

    async def inbound(message: InboundMessage) -> None:
        received.append(message)

    connector, app = await _started_telegram(inbound=inbound)
    message_handler = app.handlers[0].callback
    await message_handler(_tg_update("what do you remember?"), None)

    assert received == [
        InboundMessage(
            surface="telegram", handle="@vedant", text="what do you remember?", chat_ref="42"
        )
    ]
    # non-text updates are ignored quietly
    empty = SimpleNamespace(
        effective_message=SimpleNamespace(text="", chat_id=1), effective_user=None
    )
    await message_handler(empty, None)
    assert len(received) == 1


async def test_telegram_a_reply_carries_what_it_answers() -> None:
    received: list[InboundMessage] = []

    async def inbound(message: InboundMessage) -> None:
        received.append(message)

    connector, app = await _started_telegram(inbound=inbound)
    await app.handlers[0].callback(
        _tg_update("why?", reply_to=SimpleNamespace(message_id=101, text="the invoice is sent")),
        None,
    )
    assert received == [
        InboundMessage(
            surface="telegram",
            handle="@vedant",
            text="why?",
            chat_ref="42",
            reply_to_id="101",
            reply_to_text="the invoice is sent",
        )
    ]


async def test_telegram_a_reply_to_a_file_message_names_the_file() -> None:
    """A reply to a file-bearing message used to carry no words at all —
    the answered message's text was read from .text, which is empty when
    the words live in the caption, and the file itself was invisible. The
    reply now carries the caption AND the file's own name, so a later
    share resolves by metadata instead of a guessed id."""
    received: list[InboundMessage] = []

    async def inbound(message: InboundMessage) -> None:
        received.append(message)

    connector, app = await _started_telegram(inbound=inbound)
    await app.handlers[0].callback(
        _tg_update(
            "send it back",
            reply_to=SimpleNamespace(
                message_id=101,
                text=None,
                caption="the tour",
                document=SimpleNamespace(file_name="laptop.mp4"),
            ),
        ),
        None,
    )
    assert received[0].reply_to_text == "the tour (a message that carried the file 'laptop.mp4')"

    # a photo with no caption still names the file, marker alone
    await app.handlers[0].callback(
        _tg_update("that one", reply_to=SimpleNamespace(message_id=102, photo=[{}], text=None)),
        None,
    )
    assert received[1].reply_to_text == "(a message that carried the file 'photo.jpg')"


async def test_telegram_send_answers_with_the_message_id() -> None:
    connector, app = await _started_telegram()
    await app.handlers[0].callback(_tg_update("hi"), None)  # establish the reply target

    ref = await connector.send_to_user("the deploy finished")

    assert ref == "101"
    assert app.bot.sent[-1] == {"chat_id": 42, "text": "the deploy finished", "reply_markup": None}


async def test_telegram_commands_reach_the_loop_not_the_void() -> None:
    # the handler's filter admits /commands: the loop owns the deterministic
    # vocabulary (/verify), and what it doesn't handle flows to the model
    # like any other message — being dropped at the door only made Aether
    # look deaf
    _, app = await _started_telegram()
    update = Update(
        update_id=1,
        message=Message(
            message_id=10,
            date=datetime.now(UTC),
            chat=Chat(id=42, type=Chat.PRIVATE),
            text="/verify",
        ),
    )
    assert app.handlers[0].filters.check_update(update)


async def test_telegram_present_approval_sends_buttons() -> None:
    connector, app = await _started_telegram()
    # an inbound DM establishes the reply target; before that there is nowhere to send
    await app.handlers[0].callback(_tg_update("hi"), None)
    await connector.present_approval(7, "send 'hi' to @friend")
    sent = app.bot.sent[-1]
    assert sent["text"] == "send 'hi' to @friend"
    buttons = [b for row in sent["reply_markup"].inline_keyboard for b in row]
    assert [b.callback_data for b in buttons] == ["aether:approve:7", "aether:deny:7"]


async def test_telegram_button_press_decides_and_retires_the_buttons() -> None:
    approvals = FakeApprovals()
    decisions: list[tuple[int, str]] = []

    async def on_decision(approval_id: int, decision: str) -> None:
        decisions.append((approval_id, decision))

    connector, app = await _started_telegram(approvals=approvals, decisions=on_decision)
    button_handler = app.handlers[1].callback

    answered: list[str | None] = []
    cleared: list[bool] = []

    async def answer(text: str | None = None) -> None:
        answered.append(text)

    async def edit_message_reply_markup(reply_markup=None) -> None:
        cleared.append(reply_markup is None)

    parked = await approvals.create(tool_name="mail__send_message", params={"to": "a@b.c"})
    update = SimpleNamespace(
        callback_query=SimpleNamespace(
            data=f"aether:approve:{parked.id}",
            answer=answer,
            edit_message_reply_markup=edit_message_reply_markup,
        )
    )
    await button_handler(update, None)
    assert approvals.calls == [(parked.id, APPROVED)]
    assert decisions == [(parked.id, APPROVED)]
    # a fresh decision needs no toast — the outcome message from _carry_out
    # is the acknowledgment, never an "Approval approved: handled." edit
    assert answered == [None]
    assert cleared == [True]

    # a double press: decide returns None because the row is no longer
    # pending — the executor is not re-fired, the buttons still clear, and
    # this time a toast tells the user the tap didn't do anything new
    await button_handler(update, None)
    assert decisions == [(parked.id, APPROVED)]
    assert cleared == [True, True]
    assert answered[-1] == "that request was already decided."


async def test_telegram_button_press_on_an_expired_approval_says_so() -> None:
    approvals = FakeApprovals()
    connector, app = await _started_telegram(approvals=approvals)
    button_handler = app.handlers[1].callback

    answered: list[str | None] = []

    async def answer(text: str | None = None) -> None:
        answered.append(text)

    async def edit_message_reply_markup(reply_markup=None) -> None:
        pass

    parked = await approvals.create(tool_name="mail__send_message", params={"to": "a@b.c"})
    parked.expires_at = datetime.now(UTC) - timedelta(seconds=1)  # the TTL already passed
    update = SimpleNamespace(
        callback_query=SimpleNamespace(
            data=f"aether:approve:{parked.id}",
            answer=answer,
            edit_message_reply_markup=edit_message_reply_markup,
        )
    )
    await button_handler(update, None)
    assert answered == ["that approval has expired."]


async def test_telegram_button_press_on_an_unknown_approval_says_so() -> None:
    answered: list[str | None] = []

    async def answer(text: str | None = None) -> None:
        answered.append(text)

    async def edit_message_reply_markup(reply_markup=None) -> None:
        pass

    approvals = FakeApprovals()
    approvals.result = None  # the store found no such row at all
    connector, app = await _started_telegram(approvals=approvals)
    button_handler = app.handlers[1].callback
    update = SimpleNamespace(
        callback_query=SimpleNamespace(
            data="aether:approve:999",
            answer=answer,
            edit_message_reply_markup=edit_message_reply_markup,
        )
    )
    await button_handler(update, None)
    assert answered == ["that request could not be found."]


def _tg_button_update(data: str, answered: list, cleared: list | None = None):
    async def answer(text: str | None = None) -> None:
        answered.append(text)

    async def edit_message_reply_markup(reply_markup=None) -> None:
        if cleared is not None:
            cleared.append(reply_markup is None)

    return SimpleNamespace(
        callback_query=SimpleNamespace(
            data=data, answer=answer, edit_message_reply_markup=edit_message_reply_markup
        )
    )


async def test_telegram_task_verdict_decides_and_retires_the_buttons() -> None:
    verdicts: list[tuple[int, str]] = []

    async def on_task_decision(task_id: int, verb: str) -> str | None:
        verdicts.append((task_id, verb))
        return None  # a fresh tap — the outcome is the acknowledgment

    connector, app = await _started_telegram(task_decisions=on_task_decision)
    button_handler = app.handlers[1].callback
    answered: list[str | None] = []
    cleared: list[bool] = []
    await button_handler(_tg_button_update("aether:task:approve:12", answered, cleared), None)
    assert verdicts == [(12, "approve")]
    assert answered == [None]  # no toast — the carried-out action's words arrive
    assert cleared == [True]


async def test_telegram_task_verdict_without_a_callback_says_so() -> None:
    connector, app = await _started_telegram()
    button_handler = app.handlers[1].callback
    answered: list[str | None] = []
    await button_handler(_tg_button_update("aether:task:discard:12", answered), None)
    assert answered == ["that task could not be found."]


async def test_telegram_an_odd_task_callback_is_ignored_quietly() -> None:
    connector, app = await _started_telegram()
    button_handler = app.handlers[1].callback
    answered: list[str | None] = []
    cleared: list[bool] = []
    await button_handler(_tg_button_update("aether:task:explode:12", answered, cleared), None)
    await button_handler(_tg_button_update("aether:task:approve:twelve", answered, cleared), None)
    assert answered == [None, None]
    assert cleared == []  # nothing was decided, so nothing was retired


async def test_telegram_present_result_sends_only_the_offered_verdicts() -> None:
    connector, app = await _started_telegram()
    await app.handlers[0].callback(_tg_update("hi"), None)  # establish the reply target
    await connector.present_result(3, "the digest, in plain words", ("approve", "discard"))
    sent = app.bot.sent[-1]
    assert sent["text"] == "the digest, in plain words"
    buttons = [b for row in sent["reply_markup"].inline_keyboard for b in row]
    assert [b.callback_data for b in buttons] == ["aether:task:approve:3", "aether:task:discard:3"]
    # nothing to offer — plain text, no buttons at all
    await connector.present_result(4, "a plain note")
    assert app.bot.sent[-1]["reply_markup"] is None


async def test_telegram_send_retries_through_network_blips() -> None:
    connector, app = await _started_telegram()
    app.bot = FlakyTelegramBot(failures=2)
    connector._send_backoff = 0.0
    await app.handlers[0].callback(_tg_update("hi"), None)  # establish the reply target

    await connector.send_to_user("the deploy finished")

    assert app.bot.sent == [{"chat_id": 42, "text": "the deploy finished", "reply_markup": None}]


async def test_telegram_send_gives_up_after_sustained_failure() -> None:
    connector, app = await _started_telegram()
    flaky = FlakyTelegramBot(failures=99)
    app.bot = flaky
    connector._send_backoff = 0.0
    await app.handlers[0].callback(_tg_update("hi"), None)

    with pytest.raises(NetworkError):
        await connector.send_to_user("down")
    assert flaky.failures == 99 - SEND_ATTEMPTS  # exactly the bounded attempts
    assert app.bot.sent == []


async def test_telegram_a_redelivered_update_is_processed_once() -> None:
    """Long polling's offset lives only in memory — a restart or a network
    retry re-delivers the same update id, and without the durable ledger
    the agent answers twice. The ledger decides, not the offset."""
    received: list[InboundMessage] = []

    async def inbound(message: InboundMessage) -> None:
        received.append(message)

    ledger = FakeProcessedLedger()
    _, app = await _started_telegram(inbound=inbound, processed=ledger)
    update = _tg_update("send the video back", update_id=5001)

    await app.handlers[0].callback(update, None)
    await app.handlers[0].callback(update, None)  # the redelivery

    assert [m.text for m in received] == ["send the video back"]


async def test_telegram_a_ledger_failure_never_drops_a_message() -> None:
    """The ledger is an optimization, not the source of truth — when it is
    down the update is processed anyway, because a dropped real message
    costs more than the rare duplicate that slips through."""
    received: list[InboundMessage] = []

    async def inbound(message: InboundMessage) -> None:
        received.append(message)

    ledger = FakeProcessedLedger(fail=True)
    _, app = await _started_telegram(inbound=inbound, processed=ledger)

    await app.handlers[0].callback(_tg_update("hi", update_id=7), None)

    assert [m.text for m in received] == ["hi"]


async def test_telegram_a_token_conflict_names_the_second_poller(caplog) -> None:
    """A 409 means another process is polling with the same bot token —
    the one error that is a diagnosis, not just noise, and it must say
    so instead of streaming identical failures with no explanation."""
    connector, _ = await _started_telegram()
    caplog.set_level(logging.ERROR, logger="aether.connectors.telegram")

    await connector._on_error(
        None, SimpleNamespace(error=Conflict("terminated by other getUpdates request"))
    )

    assert "another poller" in caplog.text
    # and the ordinary failure stays the one clean line it always was
    await connector._on_error(None, SimpleNamespace(error=NetworkError("blip")))
    assert "telegram update failed" in caplog.text


async def test_telegram_a_button_press_targets_the_chat_it_was_tapped_in() -> None:
    """The outcome of a tap belongs where the tap happened — a reply
    target left over from an earlier inbound in another chat must not
    steal it, and the outcome quotes the card it answers."""
    approvals = FakeApprovals()
    connector, app = await _started_telegram(approvals=approvals)
    await app.handlers[0].callback(_tg_update("hi", chat_id=42), None)  # the stale target
    parked = await approvals.create(tool_name="mail__send_message", params={"to": "a@b.c"})

    answered: list[str | None] = []

    async def answer(text: str | None = None) -> None:
        answered.append(text)

    async def edit_message_reply_markup(reply_markup=None) -> None:
        pass

    update = SimpleNamespace(
        callback_query=SimpleNamespace(
            data=f"aether:approve:{parked.id}",
            answer=answer,
            edit_message_reply_markup=edit_message_reply_markup,
            message=SimpleNamespace(chat=SimpleNamespace(id=77), message_id=555),
        )
    )
    await app.handlers[1].callback(update, None)

    await connector.send_to_user("sent the email")

    assert app.bot.sent[-1]["chat_id"] == 77  # the tapping chat, not chat 42
    assert app.bot.sent[-1]["reply_parameters"].message_id == 555  # quotes the card


async def test_telegram_disabled_never_builds_an_app() -> None:
    factory_calls: list[str] = []

    def factory(token: str):
        factory_calls.append(token)
        raise AssertionError("must not be called")

    connector = TelegramConnector(token="t", enabled=False, app_factory=factory)
    await connector.start()
    assert factory_calls == []
    await connector.stop()


# ---------------------------------------------------------------------------
# discord (fake Bot)
# ---------------------------------------------------------------------------


class FakeDiscordChannel:
    def __init__(self, channel_type) -> None:
        self.type = channel_type
        self.id = 555
        self.sent: list[dict] = []

    async def send(self, content=None, view=None, file=None) -> SimpleNamespace:
        self.sent.append({"content": content, "view": view, "file": file})
        # the way discord answers a send — the id that links a why? back
        return SimpleNamespace(id=100 + len(self.sent))


class FakeDiscordUser:
    def __init__(self, name: str, bot: bool = False) -> None:
        self.name = name
        self.bot = bot
        self.id = 99


def _dc_message(text: str, *, dm: bool = True, bot: bool = False, reply_to=None):
    channel_type = discord.ChannelType.private if dm else discord.ChannelType.text
    return SimpleNamespace(
        author=FakeDiscordUser("vedant", bot=bot),
        channel=FakeDiscordChannel(channel_type),
        content=text,
        reference=reply_to,
    )


async def _started_discord(approvals=None, decisions=None, inbound=None, task_decisions=None):
    bot = SimpleNamespace(
        events={},
        started_with=None,
        closed=False,
    )

    async def start(token: str) -> None:
        bot.started_with = token

    async def close() -> None:
        bot.closed = True

    bot.start = start
    bot.close = close

    def event(coro) -> None:
        bot.events[coro.__name__] = coro

    bot.event = event

    connector = DiscordConnector(
        token="dc-token",
        approvals=approvals,
        on_decision=decisions,
        on_inbound=inbound,
        on_task_decision=task_decisions,
        bot_factory=lambda: bot,
    )
    await connector.start()
    await asyncio.sleep(0)  # let the gateway task run to completion
    assert bot.started_with == "dc-token"
    return connector, bot


async def test_discord_registers_on_message_and_rejects_non_dms() -> None:
    received: list[InboundMessage] = []

    async def inbound(message: InboundMessage) -> None:
        received.append(message)

    connector, bot = await _started_discord(inbound=inbound)
    assert "on_message" in bot.events

    await connector.on_message(_dc_message("ping"))
    assert received == [
        InboundMessage(surface="discord", handle="vedant", text="ping", chat_ref="99")
    ]
    # channel chatter, other bots, and empty messages are ignored
    await connector.on_message(_dc_message("noise", dm=False))
    await connector.on_message(_dc_message("beep", bot=True))
    await connector.on_message(_dc_message(""))
    assert len(received) == 1


async def test_discord_a_reply_carries_what_it_answers_and_sends_answer_with_ids() -> None:
    received: list[InboundMessage] = []

    async def inbound(message: InboundMessage) -> None:
        received.append(message)

    connector, bot = await _started_discord(inbound=inbound)
    await connector.on_message(_dc_message("why?", reply_to=SimpleNamespace(message_id=77)))
    assert received[-1].reply_to_id == "77"
    assert received[-1].reply_to_text == ""  # the answered text would cost an extra fetch

    ref = await connector.send_to_user("the deploy finished")
    assert ref == "101"


async def test_discord_approval_view_buttons_decide() -> None:
    approvals = FakeApprovals()
    decisions: list[tuple[int, str]] = []

    async def on_decision(approval_id: int, decision: str) -> None:
        decisions.append((approval_id, decision))

    connector, _ = await _started_discord(approvals=approvals, decisions=on_decision)

    # an inbound DM establishes the reply channel; before that there is nowhere to send
    await connector.on_message(_dc_message("hi"))
    await connector.present_approval(3, "dm a meme")
    channel = connector._channel
    view = channel.sent[-1]["view"]
    assert isinstance(view, ApprovalView)
    assert len(view.children) == 2

    edits: list[dict] = []

    async def edit_message(content=None, view=None) -> None:
        edits.append({"content": content, "view": view})

    interaction = SimpleNamespace(response=SimpleNamespace(edit_message=edit_message))
    await view._finish(interaction, DENIED)
    assert approvals.calls == [(3, DENIED)]
    assert decisions == [(3, DENIED)]
    assert all(child.disabled for child in view.children)
    # the question text stays (no content override) — the tap only retires
    # the buttons; the outcome message is the acknowledgment
    assert edits == [{"content": None, "view": view}]


async def test_discord_approval_view_gives_feedback_on_a_stale_or_expired_tap() -> None:
    approvals = FakeApprovals()
    connector, _ = await _started_discord(approvals=approvals)
    await connector.on_message(_dc_message("hi"))

    parked = await approvals.create(tool_name="mail__send_message", params={"to": "a@b.c"})
    await connector.present_approval(parked.id, "send it?")
    view = connector._channel.sent[-1]["view"]

    edits: list[dict] = []

    async def edit_message(content=None, view=None) -> None:
        edits.append({"content": content, "view": view})

    interaction = SimpleNamespace(response=SimpleNamespace(edit_message=edit_message))
    await view._finish(interaction, APPROVED)
    assert edits[-1]["content"] is None  # fresh decision — no extra note

    # a double tap on the same (now-decided) approval gets a note, not silence
    await view._finish(interaction, APPROVED)
    assert edits[-1]["content"] == "that request was already decided."

    # a fresh approval whose TTL already passed reads as expired, not "decided"
    expired = await approvals.create(tool_name="mail__send_message", params={"to": "b@c.d"})
    expired.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await connector.present_approval(expired.id, "send it?")
    expired_view = connector._channel.sent[-1]["view"]
    await expired_view._finish(interaction, APPROVED)
    assert edits[-1]["content"] == "that approval has expired."


async def test_discord_disabled_never_builds_a_bot() -> None:
    def factory():
        raise AssertionError("must not be called")

    connector = DiscordConnector(token="t", enabled=False, bot_factory=factory)
    await connector.start()
    await connector.stop()


async def test_discord_a_results_view_builds_only_the_offered_verdicts() -> None:
    verdicts: list[tuple[int, str]] = []

    async def on_task_decision(task_id: int, verb: str) -> str | None:
        verdicts.append((task_id, verb))
        return None

    connector, _ = await _started_discord(task_decisions=on_task_decision)
    await connector.on_message(_dc_message("hi"))  # establish the reply channel
    await connector.present_result(5, "the digest, in plain words", ("approve", "discard"))
    channel = connector._channel
    sent = channel.sent[-1]
    assert sent["content"] == "the digest, in plain words"
    view = sent["view"]
    assert [child.label for child in view.children] == ["Approve", "Discard"]

    # a fresh tap retires the buttons and needs no note — the outcome
    # message from the carried-out action is the acknowledgment
    edits: list[dict] = []

    async def edit_message(content=None, view=None) -> None:
        edits.append({"content": content, "view": view})

    interaction = SimpleNamespace(response=SimpleNamespace(edit_message=edit_message))
    pressed = view.children[0].callback
    await pressed(interaction)
    assert verdicts == [(5, "approve")]
    assert all(child.disabled for child in view.children)
    assert edits == [{"content": None, "view": view}]


async def test_discord_a_stale_verdict_tap_gets_a_note_not_silence() -> None:
    async def on_task_decision(task_id: int, verb: str) -> str | None:
        return "that task was already decided."

    connector, _ = await _started_discord(task_decisions=on_task_decision)
    await connector.on_message(_dc_message("hi"))
    await connector.present_result(6, "the digest", ("discard",))
    view = connector._channel.sent[-1]["view"]
    assert [child.label for child in view.children] == ["Discard"]

    edits: list[dict] = []

    async def edit_message(content=None, view=None) -> None:
        edits.append({"content": content, "view": view})

    await view.children[0].callback(
        SimpleNamespace(response=SimpleNamespace(edit_message=edit_message))
    )
    assert edits == [{"content": "that task was already decided.", "view": view}]


# ---------------------------------------------------------------------------
# slack (fake AsyncApp + socket handler)
# ---------------------------------------------------------------------------


class FakeSlackClient:
    def __init__(self) -> None:
        self.posted: list[dict] = []
        self.updated: list[dict] = []
        self.uploads: list[dict] = []

    async def chat_postMessage(self, channel: str, text: str, blocks=None) -> dict:
        self.posted.append({"channel": channel, "text": text, "blocks": blocks})
        # the way slack answers a send — the ts that links a why? back
        return {"ts": f"1758000000.{len(self.posted):06d}"}

    async def chat_update(self, channel: str, ts: str, text: str, blocks=None) -> None:
        self.updated.append({"channel": channel, "ts": ts, "text": text, "blocks": blocks})

    async def files_upload_v2(
        self, channel: str, file: bytes, filename: str, initial_comment: str
    ) -> dict:
        self.uploads.append(
            {
                "channel": channel,
                "file": file,
                "filename": filename,
                "initial_comment": initial_comment,
            }
        )
        return {"ok": True}


class FakeSlackApp:
    def __init__(self) -> None:
        self.listeners: dict[str, object] = {}
        self.client = FakeSlackClient()

    def event(self, name: str):
        def register(fn):
            self.listeners[name] = fn
            return fn

        return register

    def action(self, action_id: str):
        def register(fn):
            self.listeners[action_id] = fn
            return fn

        return register


class FakeSlackSocket:
    def __init__(self) -> None:
        self.connected = False

    async def connect_async(self) -> None:
        self.connected = True

    async def disconnect_async(self) -> None:
        self.connected = False


def test_slack_event_to_inbound_filters() -> None:
    good = {"channel_type": "im", "user": "U123", "text": "hello", "channel": "D1"}
    assert event_to_inbound(good) == InboundMessage("slack", "U123", "hello", "D1")
    # channel noise, bot chatter, and empty events never reach the agent
    assert event_to_inbound({**good, "channel_type": "channel"}) is None
    assert event_to_inbound({**good, "subtype": "bot_message"}) is None
    assert event_to_inbound({**good, "bot_id": "B1"}) is None
    assert event_to_inbound({**good, "text": ""}) is None
    assert event_to_inbound({}) is None


def test_slack_a_thread_reply_carries_what_it_answers() -> None:
    good = {"channel_type": "im", "user": "U123", "text": "hello", "channel": "D1"}
    # a thread reply points at the message it answers — that id is what
    # links a why? back to the trace that produced it
    assert event_to_inbound({**good, "thread_ts": "123.400"}) == InboundMessage(
        "slack", "U123", "hello", "D1", reply_to_id="123.400"
    )


def test_slack_approval_blocks_carry_action_ids_and_values() -> None:
    blocks = approval_blocks(11, "dm the team")
    assert blocks[0]["text"]["text"] == "dm the team"
    elements = blocks[1]["elements"]
    assert [(e["action_id"], e["value"]) for e in elements] == [
        (APPROVE_ACTION, "11"),
        (DENY_ACTION, "11"),
    ]


async def _started_slack(approvals=None, decisions=None, inbound=None, task_decisions=None):
    app = FakeSlackApp()
    socket = FakeSlackSocket()
    connector = SlackConnector(
        bot_token="xoxb-t",
        app_token="xoxp-a",
        approvals=approvals,
        on_decision=decisions,
        on_inbound=inbound,
        on_task_decision=task_decisions,
        app_factory=lambda token: app,
        socket_factory=lambda a, tok: socket,
    )
    await connector.start()
    assert socket.connected
    assert set(app.listeners) == {"message", APPROVE_ACTION, DENY_ACTION, TASK_ACTION}
    return connector, app, socket


async def test_slack_inbound_dm_reaches_the_handler_and_replies_work() -> None:
    received: list[InboundMessage] = []

    async def inbound(message: InboundMessage) -> None:
        received.append(message)

    connector, app, _ = await _started_slack(inbound=inbound)
    await app.listeners["message"](
        {"channel_type": "im", "user": "U123", "text": "status?", "channel": "D1"},
        None,
        app.client,
    )
    await app.listeners["message"](
        {"channel_type": "channel", "user": "U123", "text": "noise", "channel": "C9"},
        None,
        app.client,
    )
    assert received == [InboundMessage("slack", "U123", "status?", "D1")]

    await connector.send_to_user("all quiet")
    assert app.client.posted[-1] == {"channel": "D1", "text": "all quiet", "blocks": None}

    await connector.present_approval(4, "dm the team")
    posted = app.client.posted[-1]
    assert posted["blocks"][1]["elements"][0]["action_id"] == APPROVE_ACTION


async def test_slack_send_answers_with_the_message_ts() -> None:
    async def inbound(message: InboundMessage) -> None:
        pass

    connector, app, _ = await _started_slack(inbound=inbound)
    await app.listeners["message"](
        {"channel_type": "im", "user": "U123", "text": "hi", "channel": "D1"},
        None,
        app.client,
    )

    ref = await connector.send_to_user("the deploy finished")

    assert ref == "1758000000.000001"


async def test_slack_button_flow_decides_and_updates_the_message() -> None:
    approvals = FakeApprovals()
    decisions: list[tuple[int, str]] = []

    async def on_decision(approval_id: int, decision: str) -> None:
        decisions.append((approval_id, decision))

    connector, app, _ = await _started_slack(approvals=approvals, decisions=on_decision)
    acked: list[bool] = []

    async def ack() -> None:
        acked.append(True)

    body = {
        "actions": [{"value": "12"}],
        "channel": {"id": "D1"},
        "message": {
            "ts": "123.456",
            "text": "dm the team?",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "dm the team?"}},
                {"type": "actions", "block_id": "aether:12"},
            ],
        },
    }
    await app.listeners[DENY_ACTION](ack, body, app.client)
    assert approvals.calls == [(12, DENIED)]
    assert decisions == [(12, DENIED)]
    assert acked == [True]
    # the tap retires the buttons and keeps the question — the outcome
    # message is the acknowledgment, never an "Approval denied: handled." edit
    updated = app.client.updated[-1]
    assert updated["text"] == "dm the team?"
    assert [b["type"] for b in updated["blocks"]] == ["section"]

    # garbage payloads are logged and dropped, never raised
    await app.listeners[APPROVE_ACTION](ack, {"actions": [{"value": "oops"}]}, app.client)
    assert approvals.calls == [(12, DENIED)]


async def test_slack_button_flow_gives_feedback_on_a_stale_or_expired_tap() -> None:
    approvals = FakeApprovals()
    connector, app, _ = await _started_slack(approvals=approvals)

    async def ack() -> None:
        pass

    def _body(approval_id: int) -> dict:
        return {
            "actions": [{"value": str(approval_id)}],
            "channel": {"id": "D1"},
            "message": {
                "ts": "123.456",
                "text": "dm the team?",
                "blocks": [
                    {"type": "section", "text": {"type": "mrkdwn", "text": "dm the team?"}},
                    {"type": "actions", "block_id": f"aether:{approval_id}"},
                ],
            },
        }

    parked = await approvals.create(tool_name="mail__send_message", params={"to": "a@b.c"})
    await app.listeners[APPROVE_ACTION](ack, _body(parked.id), app.client)
    fresh = app.client.updated[-1]
    assert [b["type"] for b in fresh["blocks"]] == ["section"]  # no note on a fresh decision

    # a double tap on the same (now-decided) approval adds a small note
    await app.listeners[APPROVE_ACTION](ack, _body(parked.id), app.client)
    stale = app.client.updated[-1]
    assert stale["blocks"][-1] == {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "_that request was already decided._"}],
    }

    # a fresh approval whose TTL already passed reads as expired
    expired = await approvals.create(tool_name="mail__send_message", params={"to": "b@c.d"})
    expired.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await app.listeners[APPROVE_ACTION](ack, _body(expired.id), app.client)
    expired_update = app.client.updated[-1]
    assert expired_update["blocks"][-1]["elements"][0]["text"] == "_that approval has expired._"


async def test_slack_without_both_tokens_stays_down() -> None:
    connector = SlackConnector(
        bot_token="xoxb", app_token="", app_factory=None, socket_factory=None
    )
    await connector.start()
    assert connector._app is None
    await connector.stop()


def test_slack_result_blocks_carry_one_action_id_and_verb_values() -> None:
    blocks = result_blocks(12, "the digest, in plain words", ("approve", "discard"))
    assert blocks[0]["text"]["text"] == "the digest, in plain words"
    elements = blocks[1]["elements"]
    # one action id for every verdict — the value carries "verb:task id"
    assert [(e["action_id"], e["value"]) for e in elements] == [
        (TASK_ACTION, "approve:12"),
        (TASK_ACTION, "discard:12"),
    ]
    assert [e["style"] for e in elements] == ["primary", "danger"]


async def test_slack_result_flow_posts_and_a_verdict_retires_the_buttons() -> None:
    verdicts: list[tuple[int, str]] = []

    async def on_task_decision(task_id: int, verb: str) -> str | None:
        verdicts.append((task_id, verb))
        return None

    connector, app, _ = await _started_slack(task_decisions=on_task_decision)
    await app.listeners["message"](
        {"channel_type": "im", "user": "U123", "text": "hi", "channel": "D1"},
        None,
        app.client,
    )

    await connector.present_result(12, "the digest, in plain words", ("approve", "discard"))
    posted = app.client.posted[-1]
    assert posted["text"] == "the digest, in plain words"
    assert posted["blocks"][1]["elements"][0]["value"] == "approve:12"

    acked: list[bool] = []

    async def ack() -> None:
        acked.append(True)

    body = {
        "actions": [{"value": "discard:12"}],
        "channel": {"id": "D1"},
        "message": {
            "ts": "123.456",
            "text": "the digest, in plain words",
            "blocks": posted["blocks"],
        },
    }
    await app.listeners[TASK_ACTION](ack, body, app.client)
    assert verdicts == [(12, "discard")]
    assert acked == [True]
    updated = app.client.updated[-1]
    assert [b["type"] for b in updated["blocks"]] == ["section"]  # buttons retired, no note

    # an odd verb or payload is logged and dropped, never raised
    await app.listeners[TASK_ACTION](ack, {"actions": [{"value": "explode:12"}]}, app.client)
    await app.listeners[TASK_ACTION](ack, {"actions": [{"value": "oops"}]}, app.client)
    assert verdicts == [(12, "discard")]


async def test_slack_a_stale_task_verdict_gets_a_small_note() -> None:
    async def on_task_decision(task_id: int, verb: str) -> str | None:
        return "that task was already decided."

    connector, app, _ = await _started_slack(task_decisions=on_task_decision)

    async def ack() -> None:
        pass

    body = {
        "actions": [{"value": "revise:7"}],
        "channel": {"id": "D1"},
        "message": {
            "ts": "123.456",
            "text": "the digest",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "the digest"}},
                {"type": "actions"},
            ],
        },
    }
    await app.listeners[TASK_ACTION](ack, body, app.client)
    updated = app.client.updated[-1]
    assert updated["blocks"][-1] == {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "_that task was already decided._"}],
    }


# ---------------------------------------------------------------------------
# shared files: photos and documents reach the agent (all platforms, fakes)
# ---------------------------------------------------------------------------

from aether.connectors.attachments import MAX_ATTACHMENT_BYTES  # noqa: E402
from aether.connectors.base import Attachment  # noqa: E402


def _tg_file_update(
    payload: bytes,
    *,
    caption: str,
    photo: bool = False,
    document: SimpleNamespace | None = None,
    voice: bool = False,
    video_note: bool = False,
    video: bool = False,
    audio: bool = False,
    name: str = "file",
    mime: str = "",
    size: int = 9,
):
    async def download():
        return bytearray(payload)

    async def get_file():
        return SimpleNamespace(download_as_bytearray=download)

    item = SimpleNamespace(file_name=name, mime_type=mime, file_size=size, get_file=get_file)
    if document is None and not photo and not voice and not video_note and not video and not audio:
        document = item
    return SimpleNamespace(
        effective_message=SimpleNamespace(
            text=None,
            caption=caption,
            chat_id=42,
            photo=[item] if photo else [],
            document=document,
            voice=item if voice else None,
            video_note=item if video_note else None,
            video=item if video else None,
            audio=item if audio else None,
            reply_to_message=None,
        ),
        effective_user=SimpleNamespace(username="vedant", id=1),
    )


async def test_telegram_a_photo_with_a_caption_flows_as_an_image() -> None:
    received: list[InboundMessage] = []

    async def inbound(message: InboundMessage) -> None:
        received.append(message)

    connector, app = await _started_telegram(inbound=inbound)
    await app.handlers[0].callback(
        _tg_file_update(b"fake-jpg", caption="is this our cat?", photo=True), None
    )
    assert received == [
        InboundMessage(
            surface="telegram",
            handle="@vedant",
            text="is this our cat?",
            chat_ref="42",
            attachments=(Attachment("image", "photo.jpg", "image/jpeg", b"fake-jpg"),),
        )
    ]


async def test_telegram_a_document_flows_classified_by_name() -> None:
    received: list[InboundMessage] = []

    async def inbound(message: InboundMessage) -> None:
        received.append(message)

    connector, app = await _started_telegram(inbound=inbound)
    await app.handlers[0].callback(
        _tg_file_update(
            b"%PDF",
            caption="the invoice",
            name="invoice.pdf",
            mime="application/pdf",
        ),
        None,
    )
    assert received[0].text == "the invoice"
    assert received[0].attachments == (
        Attachment("doc", "invoice.pdf", "application/pdf", b"%PDF"),
    )


async def test_telegram_a_voice_note_flows_as_audio() -> None:
    received: list[InboundMessage] = []

    async def inbound(message: InboundMessage) -> None:
        received.append(message)

    connector, app = await _started_telegram(inbound=inbound)
    await app.handlers[0].callback(_tg_file_update(b"ogg-bytes", caption="", voice=True), None)
    assert received[0].attachments == (Attachment("audio", "voice.ogg", "audio/ogg", b"ogg-bytes"),)


async def test_telegram_a_video_note_flows_as_video() -> None:
    received: list[InboundMessage] = []

    async def inbound(message: InboundMessage) -> None:
        received.append(message)

    connector, app = await _started_telegram(inbound=inbound)
    await app.handlers[0].callback(_tg_file_update(b"mp4-bytes", caption="", video_note=True), None)
    assert received[0].attachments == (
        Attachment("video", "video_note.mp4", "video/mp4", b"mp4-bytes"),
    )


async def test_telegram_a_video_sent_as_a_video_flows_as_video() -> None:
    # a regular mp4 rides message.video — for a long time nothing collected
    # it, so a captioned video was answered text-only, as if unsent
    received: list[InboundMessage] = []

    async def inbound(message: InboundMessage) -> None:
        received.append(message)

    connector, app = await _started_telegram(inbound=inbound)
    await app.handlers[0].callback(
        _tg_file_update(b"clip-bytes", caption="what's in this?", video=True), None
    )
    assert received[0].text == "what's in this?"
    assert received[0].attachments == (
        Attachment("video", "video.mp4", "video/mp4", b"clip-bytes"),
    )


async def test_telegram_an_audio_file_flows_as_audio() -> None:
    received: list[InboundMessage] = []

    async def inbound(message: InboundMessage) -> None:
        received.append(message)

    connector, app = await _started_telegram(inbound=inbound)
    await app.handlers[0].callback(
        _tg_file_update(b"mp3-bytes", caption="", audio=True, name="song.mp3", mime="audio/mpeg"),
        None,
    )
    assert received[0].attachments == (Attachment("audio", "song.mp3", "audio/mpeg", b"mp3-bytes"),)


async def test_telegram_a_send_quotes_the_message_it_answers() -> None:
    # every reply visually binds to the inbound it answers, so a busy chat
    # reads in order — and a send before any inbound arrived goes out
    # without the quote rather than failing
    connector, _ = await _started_telegram()
    connector._chat_ref = "42"
    await connector.send_to_user("before any inbound")  # no id yet — plain send
    connector._reply_message_id = 777
    await connector.send_to_user("answering you")
    bot = connector._app.bot
    assert "reply_parameters" not in bot.sent[0]
    quoted = bot.sent[1]["reply_parameters"]
    assert quoted.message_id == 777
    assert quoted.allow_sending_without_reply is True


async def test_telegram_oversize_and_failed_downloads_skip_not_die() -> None:
    received: list[InboundMessage] = []

    async def inbound(message: InboundMessage) -> None:
        received.append(message)

    connector, app = await _started_telegram(inbound=inbound)
    # over the cap: the file is skipped, the words still flow
    await app.handlers[0].callback(
        _tg_file_update(b"x" * 5, caption="too big", size=MAX_ATTACHMENT_BYTES + 1), None
    )
    assert received[-1].text == "too big"
    assert received[-1].attachments == ()

    # a download that raises is skipped the same way
    async def explode():
        raise RuntimeError("network down")

    broken = SimpleNamespace(file_size=5, get_file=explode)
    update = SimpleNamespace(
        effective_message=SimpleNamespace(
            text=None,
            caption="try again",
            chat_id=42,
            photo=[broken],
            document=None,
            reply_to_message=None,
        ),
        effective_user=SimpleNamespace(username="vedant", id=1),
    )
    await app.handlers[0].callback(update, None)
    assert received[-1].text == "try again"
    assert received[-1].attachments == ()

    # and a message with neither words nor a file still emits nothing
    await app.handlers[0].callback(
        _tg_file_update(b"", caption="", photo=False, document=None), None
    )
    assert len(received) == 3


async def test_telegram_missing_or_lying_size_metadata_does_not_bypass_the_cap(monkeypatch) -> None:
    """Telegram's file_size is documented Optional — a missing, zero, or
    undercounted report must never be read as proof the file is safe. The
    actual downloaded bytes are what gets checked."""
    monkeypatch.setattr("aether.connectors.telegram.MAX_ATTACHMENT_BYTES", 10)
    received: list[InboundMessage] = []

    async def inbound(message: InboundMessage) -> None:
        received.append(message)

    connector, app = await _started_telegram(inbound=inbound)
    oversized = b"x" * 11

    for size in (None, 0, 5):  # missing, zero, and an undercounted report
        await app.handlers[0].callback(_tg_file_update(oversized, caption="x", size=size), None)
        assert received[-1].attachments == (), f"size={size} must not bypass the cap"


async def test_telegram_download_cap_is_exact_at_the_boundary(monkeypatch) -> None:
    monkeypatch.setattr("aether.connectors.telegram.MAX_ATTACHMENT_BYTES", 10)
    received: list[InboundMessage] = []

    async def inbound(message: InboundMessage) -> None:
        received.append(message)

    connector, app = await _started_telegram(inbound=inbound)

    await app.handlers[0].callback(_tg_file_update(b"x" * 10, caption="at cap", size=10), None)
    assert len(received[-1].attachments) == 1

    await app.handlers[0].callback(_tg_file_update(b"x" * 11, caption="over cap", size=11), None)
    assert received[-1].attachments == ()


class FakeDiscordAttachment:
    def __init__(self, filename: str, content_type: str, size: int, payload) -> None:
        self.filename = filename
        self.content_type = content_type
        self.size = size
        self._payload = payload

    async def read(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _dc_file_message(attachments: list, *, content: str = "", dm: bool = True):
    channel_type = discord.ChannelType.private if dm else discord.ChannelType.text
    return SimpleNamespace(
        author=FakeDiscordUser("vedant", bot=False),
        channel=FakeDiscordChannel(channel_type),
        content=content,
        reference=None,
        attachments=attachments,
    )


async def test_discord_an_attachment_only_dm_flows() -> None:
    received: list[InboundMessage] = []

    async def inbound(message: InboundMessage) -> None:
        received.append(message)

    connector, _ = await _started_discord(inbound=inbound)
    png = FakeDiscordAttachment("cat.png", "image/png", 4, b"png-bytes")
    await connector.on_message(_dc_file_message([png]))
    assert received == [
        InboundMessage(
            surface="discord",
            handle="vedant",
            text="",
            chat_ref="99",
            attachments=(Attachment("image", "cat.png", "image/png", b"png-bytes"),),
        )
    ]


async def test_discord_attachment_woes_skip_not_die() -> None:
    received: list[InboundMessage] = []

    async def inbound(message: InboundMessage) -> None:
        received.append(message)

    connector, _ = await _started_discord(inbound=inbound)
    # a missing content type classifies from the filename
    mystery = FakeDiscordAttachment("photo.jpg", "", 3, b"jpg")
    # a failed read is skipped
    dead = FakeDiscordAttachment("lost.pdf", "application/pdf", 3, RuntimeError("gone"))
    # over the cap is skipped before downloading
    huge = FakeDiscordAttachment("huge.png", "image/png", MAX_ATTACHMENT_BYTES + 1, b"")
    await connector.on_message(_dc_file_message([mystery, dead, huge], content="see this?"))
    assert received[-1].text == "see this?"
    assert received[-1].attachments == (Attachment("image", "photo.jpg", "", b"jpg"),)


async def test_discord_missing_or_lying_size_metadata_does_not_bypass_the_cap(monkeypatch) -> None:
    """Discord's API normally requires `size`, but nothing stops a test
    double — or a future API quirk — from omitting or undercounting it; the
    actual downloaded bytes are what gets checked regardless."""
    monkeypatch.setattr("aether.connectors.discord.MAX_ATTACHMENT_BYTES", 10)
    received: list[InboundMessage] = []

    async def inbound(message: InboundMessage) -> None:
        received.append(message)

    connector, _ = await _started_discord(inbound=inbound)
    oversized = b"x" * 11

    for size in (None, 0, 5):  # missing, zero, and an undercounted report
        att = FakeDiscordAttachment("big.bin", "application/octet-stream", size, oversized)
        await connector.on_message(_dc_file_message([att], content="x"))
        assert received[-1].attachments == (), f"size={size} must not bypass the cap"


async def test_discord_download_cap_is_exact_at_the_boundary(monkeypatch) -> None:
    monkeypatch.setattr("aether.connectors.discord.MAX_ATTACHMENT_BYTES", 10)
    received: list[InboundMessage] = []

    async def inbound(message: InboundMessage) -> None:
        received.append(message)

    connector, _ = await _started_discord(inbound=inbound)

    at_cap = FakeDiscordAttachment("f", "application/octet-stream", 10, b"x" * 10)
    await connector.on_message(_dc_file_message([at_cap], content="x"))
    assert len(received[-1].attachments) == 1

    over_cap = FakeDiscordAttachment("f", "application/octet-stream", 11, b"x" * 11)
    await connector.on_message(_dc_file_message([over_cap], content="x"))
    assert received[-1].attachments == ()


def _slack_file_event() -> dict:
    return {
        "channel_type": "im",
        "user": "U123",
        "channel": "D1",
        "subtype": "file_share",
        "files": [
            {"id": "F1", "name": "notes.md", "mimetype": "text/markdown", "size": 5},
        ],
    }


def test_slack_a_file_share_event_with_bytes_carries_the_attachment() -> None:
    inbound = event_to_inbound(_slack_file_event(), {"F1": b"hello"})
    assert inbound is not None
    assert inbound.attachments == (Attachment("doc", "notes.md", "text/markdown", b"hello"),)


def test_slack_a_file_without_its_bytes_still_flows_without_it() -> None:
    inbound = event_to_inbound(_slack_file_event(), {})
    assert inbound is not None
    assert inbound.attachments == ()


def test_slack_odd_file_events_are_filtered() -> None:
    assert event_to_inbound({**_slack_file_event(), "bot_id": "B1"}, {"F1": b"x"}) is None
    no_files = _slack_file_event()
    no_files.pop("files")
    assert event_to_inbound(no_files) is None


async def test_slack_the_connector_downloads_and_emits() -> None:
    received: list[InboundMessage] = []

    async def inbound(message: InboundMessage) -> None:
        received.append(message)

    connector, app, _ = await _started_slack(inbound=inbound)

    async def fake_download(files):
        return {"F1": b"hello"}

    connector._download_files = fake_download  # noqa: B010
    await app.listeners["message"](_slack_file_event(), None, app.client)
    assert received == [
        InboundMessage(
            surface="slack",
            handle="U123",
            text="",
            chat_ref="D1",
            attachments=(Attachment("doc", "notes.md", "text/markdown", b"hello"),),
        )
    ]


def _patch_slack_http(monkeypatch, payload: bytes, *, fail: bool = False) -> None:
    """Routes slack.py's real httpx.AsyncClient through a MockTransport, so
    _download_files' actual streaming code runs end to end against a
    canned response instead of the network."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, content=payload)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("aether.connectors.slack.httpx.AsyncClient", client_factory)


async def test_slack_missing_or_lying_size_metadata_does_not_bypass_the_cap(monkeypatch) -> None:
    """A Slack file (e.g. an externally-shared one) can report size as 0 or
    omit it — the actual streamed bytes are what gets checked."""
    monkeypatch.setattr("aether.connectors.slack.MAX_ATTACHMENT_BYTES", 10)
    connector, _, _ = await _started_slack()
    oversized = b"x" * 11

    for size in (None, 0, 5):  # missing, zero, and an undercounted report
        _patch_slack_http(monkeypatch, oversized)
        out = await connector._download_files(
            [{"id": "F1", "name": "big.bin", "url_private_download": "https://x/f", "size": size}]
        )
        assert out == {}, f"size={size} must not bypass the cap"


async def test_slack_download_cap_is_exact_at_the_boundary(monkeypatch) -> None:
    monkeypatch.setattr("aether.connectors.slack.MAX_ATTACHMENT_BYTES", 10)
    connector, _, _ = await _started_slack()

    _patch_slack_http(monkeypatch, b"x" * 10)
    at_cap = await connector._download_files(
        [{"id": "F1", "name": "f", "url_private_download": "https://x/f", "size": 10}]
    )
    assert at_cap == {"F1": b"x" * 10}

    _patch_slack_http(monkeypatch, b"x" * 11)
    over_cap = await connector._download_files(
        [{"id": "F1", "name": "f", "url_private_download": "https://x/f", "size": 11}]
    )
    assert over_cap == {}


async def test_slack_an_interrupted_download_is_skipped_and_nothing_partial_kept(
    monkeypatch,
) -> None:
    monkeypatch.setattr("aether.connectors.slack.MAX_ATTACHMENT_BYTES", 10)
    connector, _, _ = await _started_slack()
    _patch_slack_http(monkeypatch, b"irrelevant", fail=True)

    out = await connector._download_files(
        [{"id": "F1", "name": "f", "url_private_download": "https://x/f", "size": 5}]
    )
    assert out == {}


# ---------------------------------------------------------------------------
# media out — the attachment-capable send paths (Build F)
# ---------------------------------------------------------------------------

_PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 32
_MP4 = b"\x00\x00\x00\x18ftypisom" + b"v" * 32


def _image_attachment() -> Attachment:
    return Attachment("image", "chart.png", "image/png", _PNG)


async def test_telegram_a_small_image_sends_as_a_photo_with_the_buttons_kept() -> None:
    connector, app = await _started_telegram()
    await app.handlers[0].callback(_tg_update("hi"), None)  # establish the reply target

    ref = await connector.send_to_user("here is the chart", attachment=_image_attachment())

    sent = app.bot.sent[-1]
    assert sent["photo"] == _PNG
    assert sent["caption"] == "here is the chart"
    assert sent["reply_markup"] is None
    assert ref == "101"
    # the one-tap buttons ride the photo itself, so a media-carrying ask
    # is still one tap to answer
    await connector.present_approval(
        7, "send 'the chart' to @friend?", attachment=_image_attachment()
    )
    sent = app.bot.sent[-1]
    assert sent["photo"] == _PNG
    buttons = [b for row in sent["reply_markup"].inline_keyboard for b in row]
    assert [b.callback_data for b in buttons] == ["aether:approve:7", "aether:deny:7"]


async def test_telegram_an_image_over_the_photo_cap_falls_back_to_a_document(monkeypatch) -> None:
    monkeypatch.setattr("aether.connectors.telegram.PHOTO_CAP_BYTES", 4)
    connector, app = await _started_telegram()
    await app.handlers[0].callback(_tg_update("hi"), None)

    await connector.send_to_user("a big screenshot", attachment=_image_attachment())

    sent = app.bot.sent[-1]
    assert "photo" not in sent
    assert sent["document"] == _PNG
    assert sent["filename"] == "chart.png"  # the stored name survives the trip
    assert sent["caption"] == "a big screenshot"


async def test_telegram_a_video_sends_as_a_document() -> None:
    connector, app = await _started_telegram()
    await app.handlers[0].callback(_tg_update("hi"), None)

    clip = Attachment("video", "clip.mp4", "video/mp4", _MP4)
    await connector.send_to_user("the edited clip", attachment=clip)

    sent = app.bot.sent[-1]
    assert "photo" not in sent
    assert sent["document"] == _MP4
    assert sent["filename"] == "clip.mp4"


async def test_telegram_a_file_over_the_document_cap_is_refused_honestly(monkeypatch) -> None:
    monkeypatch.setattr("aether.connectors.telegram.DOCUMENT_CAP_BYTES", 8)
    connector, app = await _started_telegram()
    await app.handlers[0].callback(_tg_update("hi"), None)

    await connector.send_to_user("here it is", attachment=_image_attachment())

    # no file send at all — the words land with a plain note in the file's
    # place, never a silent drop and never a lie that it was sent
    sent = app.bot.sent[-1]
    assert "photo" not in sent and "document" not in sent
    assert sent["text"] == "here it is\n\n(the file is too big for Telegram and was not sent)"
    # the buttons survive the refusal — the ask is still answerable
    await connector.present_result(3, "the digest", ("approve",), attachment=_image_attachment())
    sent = app.bot.sent[-1]
    assert "(the file is too big for Telegram and was not sent)" in sent["text"]
    assert sent["reply_markup"] is not None


async def test_telegram_a_text_too_long_for_a_caption_follows_as_its_own_message() -> None:
    connector, app = await _started_telegram()
    await app.handlers[0].callback(_tg_update("hi"), None)

    long_text = "x" * (CAPTION_CAP_CHARS + 1)
    await connector.send_to_user(long_text, attachment=_image_attachment())

    # the file went first, without a caption — no word was silently cut
    assert app.bot.sent[-2]["photo"] == _PNG
    assert app.bot.sent[-2]["caption"] is None
    # the words follow whole, carrying the buttons so the one-tap path
    # stays on the message the user reads
    assert app.bot.sent[-1]["text"] == long_text
    assert app.bot.sent[-1]["reply_markup"] is None


async def test_discord_an_attachment_sends_as_a_fresh_file() -> None:
    connector, bot = await _started_discord()
    await connector.on_message(_dc_message("hi"))

    ref = await connector.send_to_user("here is the chart", attachment=_image_attachment())

    sent = connector._channel.sent[-1]
    file = sent["file"]
    assert isinstance(file, discord.File)
    assert file.filename == "chart.png"
    assert file.fp.read() == _PNG
    assert sent["view"] is None
    assert ref == "101"


async def test_discord_an_approval_keeps_its_view_with_the_file() -> None:
    connector, bot = await _started_discord()
    await connector.on_message(_dc_message("hi"))

    await connector.present_approval(7, "send the chart?", attachment=_image_attachment())

    sent = connector._channel.sent[-1]
    assert isinstance(sent["file"], discord.File)
    assert isinstance(sent["view"], ApprovalView)


async def test_discord_an_upload_over_the_cap_is_refused_honestly(monkeypatch) -> None:
    monkeypatch.setattr("aether.connectors.discord.UPLOAD_CAP_BYTES", 8)
    connector, bot = await _started_discord()
    await connector.on_message(_dc_message("hi"))

    await connector.send_to_user("here it is", attachment=_image_attachment())

    sent = connector._channel.sent[-1]
    assert sent["file"] is None
    assert sent["content"] == "here it is\n\n(the file is too big for Discord and was not sent)"


async def test_slack_a_file_sends_with_the_words_as_its_initial_comment() -> None:
    connector, app, _ = await _started_slack()
    await app.listeners["message"](
        {"channel_type": "im", "user": "U123", "text": "hi", "channel": "D1"},
        None,
        app.client,
    )

    ref = await connector.send_to_user("here is the chart", attachment=_image_attachment())

    assert app.client.uploads == [
        {
            "channel": "D1",
            "file": _PNG,
            "filename": "chart.png",
            "initial_comment": "here is the chart",
        }
    ]
    assert app.client.posted == []  # the words rode the file, no double send
    # a file share has no ts — the send happened, it just can't be linked
    assert ref is None


async def test_slack_an_approval_file_is_followed_by_the_button_message() -> None:
    connector, app, _ = await _started_slack()
    await app.listeners["message"](
        {"channel_type": "im", "user": "U123", "text": "hi", "channel": "D1"},
        None,
        app.client,
    )

    await connector.present_approval(7, "send the chart?", attachment=_image_attachment())

    # files carry no blocks — the ask rides the file, the one-tap buttons
    # follow in a plain message so the verdict stays one tap
    assert app.client.uploads[-1]["initial_comment"] == "send the chart?"
    posted = app.client.posted[-1]
    assert posted["text"] == "send the chart?"
    assert posted["blocks"] == approval_blocks(7, "send the chart?")


async def test_slack_a_result_file_is_followed_by_the_verdict_buttons() -> None:
    connector, app, _ = await _started_slack()
    await app.listeners["message"](
        {"channel_type": "im", "user": "U123", "text": "hi", "channel": "D1"},
        None,
        app.client,
    )

    await connector.present_result(
        3, "the captioned video", ("approve", "discard"), attachment=_image_attachment()
    )

    assert app.client.uploads[-1]["initial_comment"] == "the captioned video"
    posted = app.client.posted[-1]
    assert posted["blocks"] == result_blocks(3, "the captioned video", ("approve", "discard"))


# ---------------------------------------------------------------------------
# the fanout: media rides to the chat connectors only, only when present
# ---------------------------------------------------------------------------


class RecordingConnector:
    """A media-aware surface double: records every kwarg the fanout hands
    it, so the tests can see exactly when media rides along."""

    def __init__(self, name: str = "recording") -> None:
        self.name = name
        self.sends: list[dict] = []
        self.approvals: list[dict] = []
        self.results: list[dict] = []

    async def send_to_user(self, text: str, **kwargs) -> str:
        self.sends.append({"text": text, **kwargs})
        return "r1"

    async def present_approval(self, approval_id: int, text: str, **kwargs) -> None:
        self.approvals.append({"approval_id": approval_id, "text": text, **kwargs})

    async def present_result(self, task_id: int, text: str, options=(), **kwargs) -> None:
        self.results.append({"task_id": task_id, "text": text, "options": tuple(options), **kwargs})


class RecordingHub:
    def __init__(self) -> None:
        self.broadcasts: list[str] = []

    async def broadcast(self, text: str, *, persist: bool = True) -> None:
        self.broadcasts.append(text)


async def test_the_fanout_passes_media_only_when_present() -> None:
    fake = FakeSurfaceConnector()
    recording = RecordingConnector()
    fanout = SurfaceFanout([fake, recording])

    # no media: neither surface sees the kwarg at all — the untouched
    # FakeSurfaceConnector (no attachment param) proves it
    await fanout.send_to_user("a plain note")
    assert fake.sent == ["a plain note"]
    assert recording.sends == [{"text": "a plain note"}]

    # media present: the media-aware surface gets it. A surface that
    # never learned attachments (only the untouched test double — every
    # production connector accepts it) fails into the fanout's
    # per-connector guard, and one dying surface never takes the loop
    # down with it
    await fanout.send_to_user("with media", attachment=_image_attachment())
    assert fake.sent == ["a plain note"]
    assert recording.sends[-1] == {"text": "with media", "attachment": _image_attachment()}


async def test_the_fanout_carries_media_on_approvals_and_results_too() -> None:
    recording = RecordingConnector()
    fanout = SurfaceFanout([recording])

    await fanout.present_approval(7, "send the chart?", attachment=_image_attachment())
    assert recording.approvals == [
        {"approval_id": 7, "text": "send the chart?", "attachment": _image_attachment()}
    ]

    await fanout.present_result(3, "the digest", ("approve",), attachment=_image_attachment())
    assert recording.results == [
        {
            "task_id": 3,
            "text": "the digest",
            "options": ("approve",),
            "attachment": _image_attachment(),
        }
    ]


async def test_the_hub_and_transcript_stay_text_only() -> None:
    fake = FakeSurfaceConnector()
    recording = RecordingConnector()
    hub = RecordingHub()
    history: list[tuple[str, str, str]] = []

    async def append(name: str, direction: str, text: str) -> None:
        history.append((name, direction, text))

    fanout = SurfaceFanout([fake, recording], hub=hub, history=SimpleNamespace(append=append))

    refs = await fanout.send_to_user("with media", attachment=_image_attachment())

    # the words (not the bytes) reached the hub broadcast and the
    # transcript; the media went to the chat connectors alone. The
    # attachment-less fake surface is absent from both — its failed send
    # has no ref, so no transcript line, the same as any dying surface
    assert hub.broadcasts == ["with media"]
    assert history == [("recording", "out", "with media")]
    assert refs == {"recording": "r1"}
