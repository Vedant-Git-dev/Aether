"""Plain-language classification of LLM provider failures.

The agent loop never shows a raw provider error to the user. This module
turns one into a short sentence that names the cause in everyday words,
says whether the turn can be tried again, and how long the loop should
wait before trying.

Classification walks the exception chain because every adapter wraps the
SDK's own error in a `ProviderError` (`raise ... from exc`): the status
code and retry hint live on the inner error, the plain wrapper only
carries the text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HTTP_STATUS = re.compile(r"(?<!\d)([45]\d\d)(?!\d)")

# gRPC codes google's older client libraries put on `.code` — mapped to the
# HTTP status they mean, so one classifier serves every adapter.
_GRPC_HTTP = {
    4: 504,  # DEADLINE_EXCEEDED
    7: 403,  # PERMISSION_DENIED
    8: 429,  # RESOURCE_EXHAUSTED
    13: 500,  # INTERNAL
    14: 503,  # UNAVAILABLE
    16: 401,  # UNAUTHENTICATED
}

_AUTH_STATUSES = frozenset({401, 403})
_SERVER_STATUSES = frozenset({500, 502, 503, 504, 529})
_REQUEST_STATUSES = frozenset({400, 413, 422})

_MISSING_WORDS = (
    "is not set",
    "not configured",
    "no api key",
    "missing api key",
    "unknown provider",
    "no provider",
)
_AUTH_WORDS = (
    "api key",
    "api_key",
    "apikey",
    "unauthorized",
    "unauthenticated",
    "authentication",
    "permission denied",
    "invalid key",
    "invalid api",
    "not authorized",
)
_RATE_WORDS = (
    "rate limit",
    "rate-limit",
    "ratelimit",
    "too many requests",
    "quota",
    "resource exhausted",
    "resource_exhausted",
)
_SERVER_WORDS = (
    "overloaded",
    "service unavailable",
    "server error",
    "internal server error",
    "bad gateway",
    "gateway timeout",
)
_UNREACHABLE_WORDS = (
    "connection",
    "connect",
    "timed out",
    "timeout",
    "network",
    "unreachable",
    "getaddrinfo",
    "name resolution",
    "socket",
)
_REQUEST_WORDS = (
    "context length",
    "context window",
    "too many tokens",
    "maximum context",
    "invalid request",
    "unsupported",
    "bad request",
    "request too large",
)


@dataclass(frozen=True)
class ProviderFailure:
    """One provider failure, said in words a person can act on.

    `cause` is the sentence the user reads. `retryable` says whether the
    loop should try the same turn again by itself; `retry_after` is how
    many seconds to wait before that first retry.
    """

    kind: str
    cause: str
    retryable: bool
    retry_after: float = 0.0

    def message(self, delay: float | None = None) -> str:
        """The plain reply for this failure. A retryable failure also says
        when the loop will try again, so silence never looks like a drop."""
        if not self.retryable:
            return self.cause
        wait = human_delay(self.retry_after if delay is None else delay)
        return f"{self.cause} I'll try again in {wait} and finish what you asked."

    @property
    def exhausted(self) -> str:
        """The final word when every retry has failed."""
        return (
            "The AI service still isn't answering after several tries, so I've "
            "stopped retrying. Nothing else is affected — ask me again when "
            "you're ready."
        )


def human_delay(seconds: float) -> str:
    """A short, rounded duration: 'about 30 seconds', 'about 2 minutes'."""
    seconds = max(1, int(round(seconds)))
    if seconds < 10:
        return f"about {seconds} second" + ("" if seconds == 1 else "s")
    if seconds < 60:
        return f"about {int(round(seconds / 5.0) * 5)} seconds"
    minutes = max(1, round(seconds / 60))
    return f"about {minutes} minute" + ("" if minutes == 1 else "s")


def _chain(exc: BaseException) -> list[BaseException]:
    """The exception and its causes/contexts, bounded so a malformed
    chain can never spin."""
    out: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(out) < 8:
        seen.add(id(current))
        out.append(current)
        current = current.__cause__ or current.__context__
    return out


def _as_status(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if 100 <= value <= 599 else None
    if isinstance(value, str):
        if value.isdigit():
            number = int(value)
            return number if 100 <= number <= 599 else None
        match = _HTTP_STATUS.search(value)
        return int(match.group(1)) if match else None
    inner = getattr(value, "value", None)
    if isinstance(inner, tuple) and inner and isinstance(inner[0], int):
        return _GRPC_HTTP.get(inner[0])
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return _GRPC_HTTP.get(
            {
                "DEADLINE_EXCEEDED": 4,
                "PERMISSION_DENIED": 7,
                "RESOURCE_EXHAUSTED": 8,
                "INTERNAL": 13,
                "UNAVAILABLE": 14,
                "UNAUTHENTICATED": 16,
            }.get(name.upper(), 0)
        )
    return None


def _status_of(exc: BaseException) -> int | None:
    for attr in ("status_code", "status", "http_status", "code"):
        status = _as_status(getattr(exc, attr, None))
        if status is not None:
            return status
    response = getattr(exc, "response", None)
    if response is not None:
        for attr in ("status_code", "status"):
            status = _as_status(getattr(response, attr, None))
            if status is not None:
                return status
    match = _HTTP_STATUS.search(str(exc))
    return int(match.group(1)) if match else None


def _retry_hint(chain: list[BaseException], text: str) -> float | None:
    """The server's own suggested wait, from a header or a message hint."""
    for exc in chain:
        for source in (exc, getattr(exc, "response", None)):
            headers = getattr(source, "headers", None)
            if headers is None:
                continue
            for key in ("retry-after", "Retry-After"):
                try:
                    raw = headers.get(key)
                except Exception:
                    raw = None
                if raw:
                    try:
                        return max(1.0, min(float(raw), 120.0))
                    except (TypeError, ValueError):
                        pass
        value = getattr(exc, "retry_after", None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(1.0, min(float(value), 120.0))
    for pattern in (
        r"retry (?:in|after) ([\d.]+)\s*s",
        r"try again in ([\d.]+)\s*s",
        r"retry_delay[^0-9]*([\d.]+)",
    ):
        match = re.search(pattern, text)
        if match:
            try:
                return max(1.0, min(float(match.group(1)) + 1.0, 120.0))
            except ValueError:
                continue
    return None


def _says(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def classify_provider_failure(exc: BaseException) -> ProviderFailure:
    """Turn one provider exception into the plain sentence the user reads."""
    chain = _chain(exc)
    text = " ".join(str(item) for item in chain).lower()
    status = next(
        (found for item in chain if (found := _status_of(item)) is not None),
        None,
    )
    hint = _retry_hint(chain, text)

    # a missing or rejected key is the user's to fix, never a retry loop
    if status in _AUTH_STATUSES or _says(text, _AUTH_WORDS):
        if _says(text, _MISSING_WORDS):
            return ProviderFailure(
                "auth_missing",
                "My key for the AI service isn't set up, so I can't ask it "
                "anything. Add it in Settings under Model providers.",
                False,
            )
        return ProviderFailure(
            "auth_rejected",
            "The AI service rejected my key. Check it in Settings under "
            "Model providers, then ask me again.",
            False,
        )
    if _says(text, ("unknown provider", "not configured", "no provider")):
        return ProviderFailure(
            "config",
            "My model settings point at something I can't use. Check the "
            "model settings in Settings, then ask me again.",
            False,
        )

    if status == 429 or _says(text, _RATE_WORDS):
        return ProviderFailure(
            "rate_limit",
            "The AI service is limiting how many requests I can make right now.",
            True,
            hint or 30.0,
        )
    if status in _SERVER_STATUSES or _says(text, _SERVER_WORDS):
        return ProviderFailure(
            "overloaded",
            "The AI service is having trouble on its side right now.",
            True,
            hint or 20.0,
        )
    if _says(text, _UNREACHABLE_WORDS) or any(
        "Connection" in type(item).__name__ or "Timeout" in type(item).__name__ for item in chain
    ):
        return ProviderFailure(
            "unreachable",
            "I couldn't reach the AI service just now.",
            True,
            hint or 15.0,
        )
    if status in _REQUEST_STATUSES or _says(text, _REQUEST_WORDS):
        return ProviderFailure(
            "request",
            "The AI service refused that request — it was probably too large "
            "for the model. Try a shorter message, or pick a different model "
            "in Settings.",
            False,
        )
    return ProviderFailure(
        "unknown",
        "I hit an unexpected problem talking to the AI service. Nothing else "
        "is affected — ask me again in a moment.",
        False,
    )
