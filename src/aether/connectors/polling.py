"""Poll scheduling: a little jitter, and a backoff that never gives up.

Several feeds waking on the same fixed minute hammer a provider in lockstep
and make a rate limit worse. An interval spread by a small random share,
plus exponential backoff after a failure, is the whole policy. It is pure
so it can be tested without a clock or a network.
"""

from __future__ import annotations

import random
from collections.abc import Callable

MAX_BACKOFF = 4.0


def jittered_interval(
    minutes: float,
    jitter: float = 0.15,
    *,
    rand: Callable[[], float] = random.random,
) -> float:
    """One interval in minutes, spread by +/- `jitter` of itself."""
    if minutes <= 0:
        return 0.0
    spread = max(0.0, min(0.5, float(jitter)))
    factor = 1.0 + (float(rand()) * 2.0 - 1.0) * spread
    return max(0.01, float(minutes) * factor)


def backoff_factor(failures: int, *, cap: float = MAX_BACKOFF) -> float:
    """1x for a healthy poll, 2x, 4x, then no further — a dead feed is
    retried eventually, never once per tick forever at full speed."""
    try:
        count = max(0, int(failures))
    except (TypeError, ValueError):
        count = 0
    return min(cap, float(2**count))


def next_poll_delay(
    minutes: float,
    *,
    failures: int = 0,
    jitter: float = 0.15,
    rand: Callable[[], float] = random.random,
) -> float:
    """The full delay before the next poll, jitter and backoff included."""
    return jittered_interval(minutes, jitter, rand=rand) * backoff_factor(failures)
