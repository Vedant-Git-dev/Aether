"""restart.py tests — the self-restart switch. No real server: a stub with
the one flag request() flips.

The module is a small box of globals by design (one restart in flight per
process — the serve loop owns this state in production), so every test
resets it first and cancels any armed task after, the same way main()'s loop
hands the state back and forth between iterations.
"""

from __future__ import annotations

import pytest

from aether import restart


class StubServer:
    """uvicorn.Server double: just the should_exit flag request() flips."""

    def __init__(self) -> None:
        self.should_exit = False


@pytest.fixture(autouse=True)
def _clean_module_state():
    restart._server = None
    restart._pending = False
    if restart._task is not None:
        restart._task.cancel()
    restart._task = None
    yield
    if restart._task is not None:
        restart._task.cancel()
    restart._server = None
    restart._pending = False
    restart._task = None


def test_request_without_a_server_is_false_and_never_raises() -> None:
    # a sync test on purpose: no running event loop anywhere — a restart
    # request must never crash the process making it
    assert restart.request() is False
    assert restart.take_pending() is False  # nothing was armed


async def test_request_flips_the_registered_server_after_the_delay() -> None:
    server = StubServer()
    restart.register(server)

    assert restart.request(delay_seconds=0.01) is True
    assert server.should_exit is False  # not yet — the current turn is still landing its words

    task = restart._task
    assert task is not None
    await task
    assert server.should_exit is True

    assert restart.take_pending() is True  # the serve loop iterates
    assert restart.take_pending() is False  # the flag is consumed exactly once


async def test_a_second_request_while_pending_is_still_one_restart() -> None:
    server = StubServer()
    restart.register(server)

    assert restart.request(delay_seconds=0.05) is True
    task = restart._task
    assert restart.request(delay_seconds=0.05) is True  # idempotent, not a second task
    assert restart._task is task

    await task
    assert server.should_exit is True


async def test_register_replaces_the_server_between_serve_iterations() -> None:
    first = StubServer()
    restart.register(first)
    restart.request(delay_seconds=0.01)

    # the serve loop iterated: a fresh server is registered before serving,
    # and the armed restart lands on the server actually running
    second = StubServer()
    restart.register(second)
    assert restart._task is not None
    await restart._task

    assert second.should_exit is True
    assert first.should_exit is False  # the stale handle was never told to exit
