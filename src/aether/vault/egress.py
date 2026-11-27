"""Keep private vault work and its descendants off generic outbound paths."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from .errors import VaultProtocolError

_private_work = ContextVar("aether_private_work", default=False)


@contextmanager
def private_work() -> Iterator[None]:
    token = _private_work.set(True)
    try:
        yield
    finally:
        _private_work.reset(token)


def require_unscoped() -> None:
    if _private_work.get():
        raise VaultProtocolError("bad_scope")
