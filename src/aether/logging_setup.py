"""Logging configuration for Aether.

Two safety nets run on every record before a handler sees it: secrets never
ride a URL into the access log, and user content — addresses, contact links,
home paths, filenames and quoted snippets — is rewritten even if a log line
somehow carries it. The code is written to log ids and counts only; the
scrubber is the belt to that suspenders.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

# any token riding a URL — the WebSocket handshake (browsers can't set
# headers there), a curl pasted by hand — matches this; the value never
# reaches a handler
_TOKEN_IN_URL = re.compile(r"(?i)(token=)[^&\s\"']+")

# user content that must never appear in a log line. These are deliberately
# broad: a false positive costs a redacted word, a false negative leaks the
# user's world into a log sink.
_CONTACT_LINK = re.compile(r"(?i)\b(?:mailto|tel):\S+")
_EMAIL = re.compile(r"(?i)\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_HOME_PATH = re.compile(r"(?i)(?:/home/|/Users/)[^\s\"']+|[A-Za-z]:\\Users\\[^\s\"']+")
_FILENAME = re.compile(
    r"(?i)\b[\w][\w .\-]{0,60}\.(?:pdf|docx?|xlsx?|pptx?|csv|tsv|txt|md|png|jpe?g|gif|webp|svg"
    r"|mp4|mov|avi|mkv|mp3|wav|m4a|ogg|zip|tar|gz)\b"
)
_SNIPPET = re.compile(r'"[^"\n]{25,}"|\'[^\'\n]{25,}\'')


class _NoTokensInLogs(logging.Filter):
    """Rewrites tokens in URLs and user content on the record itself, before
    any handler sees it. The name predates the content scrub; the token job
    is the original one, and both live here because they share the walk."""

    # walks whatever shape a logger put in msg/args — the scrub returns the
    # same structure with every value rewritten
    def _scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            value = _TOKEN_IN_URL.sub(r"\1***", value)
            value = _CONTACT_LINK.sub("[contact]", value)
            value = _EMAIL.sub("[address]", value)
            value = _HOME_PATH.sub("[home]", value)
            return _FILENAME.sub("[file]", _SNIPPET.sub("[text]", value))
        if isinstance(value, dict):
            return {k: self._scrub(v) for k, v in value.items()}
        if isinstance(value, tuple):
            return tuple(self._scrub(v) for v in value)
        if isinstance(value, list):
            return [self._scrub(v) for v in value]
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._scrub(record.msg)
        record.args = self._scrub(record.args)
        return True


def _redact_tokens_from_access_log() -> None:
    """Idempotent — the serve loop rebuilds the app on every chat-made
    restart, and each pass must not stack another filter."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _NoTokensInLogs) for f in access.filters):
        access.addFilter(_NoTokensInLogs())


def _redact_content_from_handlers() -> None:
    """The same scrub on the root handlers, so a record from any logger that
    propagates is scrubbed even when it never passed a logger-level filter."""
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, _NoTokensInLogs) for f in handler.filters):
            handler.addFilter(_NoTokensInLogs())


def configure_logging() -> None:
    level = os.environ.get("AETHER_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # Quiet the chatty third-party loggers
    for name in ("httpx", "httpcore", "asyncio", "matplotlib"):
        logging.getLogger(name).setLevel(logging.WARNING)
    _redact_tokens_from_access_log()
    _redact_content_from_handlers()
