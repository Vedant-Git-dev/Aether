"""Provider failures said in plain words, unit: the classifier behind the
one sentence a dead model turn sends.

The observed symptom this file pins: a turn that died on the provider left
the user with "something went wrong" and no way to tell a rate limit from
a rejected key — and no idea whether the ask would ever be picked up.
"""

from __future__ import annotations

from aether.llm.base import ProviderError
from aether.llm.failures import classify_provider_failure, human_delay


class _HTTPError(Exception):
    """The shape every SDK's status error has: a status code and headers."""

    def __init__(self, message: str, status_code: int, retry_after: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.headers = {} if retry_after is None else {"retry-after": retry_after}


def _wrapped(inner: Exception) -> ProviderError:
    error = ProviderError(f"provider call failed: {inner}")
    error.__cause__ = inner
    return error


def test_a_rate_limit_is_retryable_and_says_when() -> None:
    failure = classify_provider_failure(_wrapped(_HTTPError("slow down", 429)))

    assert failure.kind == "rate_limit"
    assert failure.retryable is True
    assert failure.retry_after == 30.0
    assert failure.message() == (
        "The AI service is limiting how many requests I can make right now. "
        "I'll try again in about 30 seconds and finish what you asked."
    )


def test_the_server_s_own_retry_hint_wins() -> None:
    failure = classify_provider_failure(_wrapped(_HTTPError("slow down", 429, retry_after="7")))

    assert failure.retry_after == 7.0


def test_a_retry_hint_in_the_message_is_read_too() -> None:
    failure = classify_provider_failure(
        ProviderError("gemini call failed: 429 quota exceeded, please retry in 12.5s")
    )

    assert failure.kind == "rate_limit"
    assert failure.retry_after == 13.5


def test_an_overloaded_server_is_retryable() -> None:
    failure = classify_provider_failure(_wrapped(_HTTPError("overloaded_error", 529)))

    assert failure.kind == "overloaded"
    assert failure.retryable is True


def test_an_unreachable_provider_is_retryable() -> None:
    failure = classify_provider_failure(
        _wrapped(ConnectionError("connection refused by the provider"))
    )

    assert failure.kind == "unreachable"
    assert failure.retryable is True


def test_a_missing_key_says_what_to_fix_and_never_retries() -> None:
    failure = classify_provider_failure(
        ProviderError(
            "llm.provider is anthropic but ANTHROPIC_API_KEY is not set — "
            "add it to .env, or point llm.provider at a different provider"
        )
    )

    assert failure.kind == "auth_missing"
    assert failure.retryable is False
    assert "isn't set up" in failure.message()
    assert "Settings under Model providers" in failure.message()


def test_a_rejected_key_says_what_to_fix_and_never_retries() -> None:
    failure = classify_provider_failure(_wrapped(_HTTPError("unauthorized", 401)))

    assert failure.kind == "auth_rejected"
    assert failure.retryable is False
    assert "rejected my key" in failure.message()


def test_a_request_the_model_refused_is_not_retried() -> None:
    failure = classify_provider_failure(
        ProviderError("openai call failed: maximum context length exceeded")
    )

    assert failure.kind == "request"
    assert failure.retryable is False
    assert "too large" in failure.message()


def test_an_unknown_failure_is_honest_and_does_not_leak_the_error() -> None:
    failure = classify_provider_failure(ProviderError("openai: response contained no choices"))

    assert failure.kind == "unknown"
    assert failure.retryable is False
    assert "unexpected problem" in failure.message()
    assert "choices" not in failure.message()


def test_delays_read_like_a_person_says_them() -> None:
    assert human_delay(1) == "about 1 second"
    assert human_delay(30) == "about 30 seconds"
    assert human_delay(13.5) == "about 15 seconds"
    assert human_delay(60) == "about 1 minute"
    assert human_delay(150) == "about 2 minutes"
