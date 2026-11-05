"""Poll scheduling: jitter spreads the herd, backoff eases a dead feed."""

from __future__ import annotations

from aether.connectors.polling import backoff_factor, jittered_interval, next_poll_delay


def test_jitter_stays_within_its_band() -> None:
    low = jittered_interval(10.0, 0.2, rand=lambda: 0.0)
    high = jittered_interval(10.0, 0.2, rand=lambda: 1.0)
    middle = jittered_interval(10.0, 0.2, rand=lambda: 0.5)

    assert round(low, 3) == 8.0
    assert round(high, 3) == 12.0
    assert round(middle, 3) == 10.0


def test_jitter_is_clamped_and_zero_minutes_is_zero() -> None:
    assert jittered_interval(0.0, 0.2) == 0.0
    assert jittered_interval(10.0, 5.0, rand=lambda: 1.0) == 15.0  # spread clamps at 0.5


def test_backoff_doubles_then_stops() -> None:
    assert backoff_factor(0) == 1.0
    assert backoff_factor(1) == 2.0
    assert backoff_factor(2) == 4.0
    assert backoff_factor(9) == 4.0


def test_next_delay_combines_both() -> None:
    assert round(next_poll_delay(10.0, failures=1, jitter=0.0), 3) == 20.0
