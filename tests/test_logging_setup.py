"""The access-log scrub, machine-checked: the token rides the query string
in the places a browser can't send headers (the WebSocket handshake) and
in any curl pasted by hand — the value must never reach a log handler."""

from __future__ import annotations

import logging

from aether.logging_setup import _NoTokensInLogs, configure_logging


def _access_log_filters() -> int:
    return sum(isinstance(f, _NoTokensInLogs) for f in logging.getLogger("uvicorn.access").filters)


def test_the_token_never_reaches_a_handler(caplog) -> None:
    configure_logging()
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        # uvicorn's own access-log shape: the whole request line, query
        # string included, arrives as format args on the record
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d',
            *("127.0.0.1", "GET", "/api/traces?token=sekrit", "1.1", 200),
        )
    assert "sekrit" not in caplog.text
    assert "token=***" in caplog.text


def test_a_token_logged_mid_query_stops_at_the_ampersand(caplog) -> None:
    configure_logging()
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        logging.getLogger("uvicorn.access").info(
            "WebSocket /ws/chat?token=sekrit&other=1 connected"
        )
    assert "sekrit" not in caplog.text
    assert "token=***&other=1" in caplog.text


def test_lines_without_a_token_pass_through_untouched(caplog) -> None:
    configure_logging()
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d',
            *("127.0.0.1", "GET", "/api/health", "1.1", 200),
        )
    assert "GET /api/health" in caplog.text


def test_installing_twice_stacks_no_second_filter() -> None:
    configure_logging()
    before = _access_log_filters()
    configure_logging()  # the serve loop calls this again on every restart
    assert _access_log_filters() == before == 1


# -- the content safety net -------------------------------------------------


def _scrubbed(msg: object, args: object = ()) -> str:
    record = logging.LogRecord("aether", logging.INFO, __file__, 1, msg, args, None)
    _NoTokensInLogs().filter(record)
    return record.getMessage()


def test_addresses_links_and_files_never_reach_a_log() -> None:
    out = _scrubbed(
        "mail from %s about %s, reachable at %s",
        ("friend@example.com", "invoice.pdf", "mailto:friend@example.com"),
    )
    assert "friend@example.com" not in out
    assert "invoice.pdf" not in out
    assert "[address]" in out
    assert "[file]" in out


def test_home_paths_and_snippets_never_reach_a_log() -> None:
    out = _scrubbed(
        "wrote %s and said %s",
        ("/home/vedant/notes.txt", '"the subject line runs long enough to count"'),
    )
    assert "/home/vedant" not in out
    assert "the subject line runs long enough" not in out
    assert "[home]" in out
    assert "[text]" in out


def test_ids_and_counts_pass_through_the_scrubber() -> None:
    out = _scrubbed("event %d scored %.1f for %s", (42, 7.5, "gmail"))
    assert out == "event 42 scored 7.5 for gmail"


async def test_provider_sdk_logs_cannot_emit_capture_secrets_at_debug(caplog, monkeypatch):
    import json
    from unittest.mock import AsyncMock

    from telegram import Bot, User
    from telegram.request import HTTPXRequest

    token = "123456:synthetic-credential"
    incoming = "pvt-note"
    payload = {
        "ok": True,
        "result": [
            {
                "update_id": 17,
                "message": {
                    "message_id": 1,
                    "date": 0,
                    "chat": {"id": 42, "type": "private"},
                    "from": {"id": 42, "first_name": "Fixture", "is_bot": False},
                    "text": incoming,
                },
            }
        ],
    }
    monkeypatch.setenv("AETHER_LOG_LEVEL", "DEBUG")
    with caplog.at_level(logging.DEBUG):
        configure_logging()
        request = HTTPXRequest()
        monkeypatch.setattr(
            HTTPXRequest, "do_request", AsyncMock(return_value=(200, json.dumps(payload).encode()))
        )
        bot = Bot(token, request=request, get_updates_request=request)
        monkeypatch.setattr(Bot, "bot", property(lambda _: User(7, "Fixture bot", True)))
        [update] = await bot.get_updates()
        assert update.message.text == incoming
        logging.getLogger("discord.http").debug("GET returned %s", {"content": incoming})
        logging.getLogger("aether.test").debug("Capture debug is enabled.")
        await request.shutdown()

    assert "Capture debug is enabled." in caplog.text
    assert all(
        value not in record.getMessage() for record in caplog.records for value in (incoming, token)
    )


async def test_unprovisioned_provider_capture_fails_before_contacting_storage(monkeypatch):
    import pytest

    from aether import main
    from aether.config import Settings

    async def forbidden(*args, **kwargs):
        pytest.fail("startup reached storage without an ingress identity key")

    monkeypatch.setattr(main, "create_pool", forbidden)
    app = main.create_app(
        settings=Settings.without_env_file(
            database_url="postgresql:///fixture",
            telegram_bot_token="123456:synthetic-credential",
            telegram_ingress_owners=frozenset({42}),
        )
    )
    with pytest.raises(main.StartupError, match="AETHER_VAULT_INGRESS_IDENTITY_KEY"):
        async with app.router.lifespan_context(app):
            pytest.fail("provider capture started without an ingress identity key")
