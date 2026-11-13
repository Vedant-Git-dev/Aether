"""Where a turn came from — the provenance every mutation needs.

The harness, not the model, decides this. A turn that began with the owner's
own message carries ``owner_message`` with the message ids sealed on the
record it creates; an approved card carries ``approved_call`` with the
approval id. Anything else — an unprompted tick, a scheduled run, an alert's
own action, a tool result — cannot create, change or delete an alert or
forget a memory.

The model never supplies this. It travels in a context variable set at the
turn's entry point, so every tool call in that turn (including the calls the
tool loop runs concurrently) reads the same value without threading a
parameter through every handler.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class OriginKind(StrEnum):
    OWNER_MESSAGE = "owner_message"
    APPROVED_CALL = "approved_call"
    SCHEDULED = "scheduled"
    ALERT_ACTION = "alert_action"
    UNPROMPTED = "unprompted"
    MIGRATION = "migration"


_MUTATING_KINDS = frozenset({OriginKind.OWNER_MESSAGE, OriginKind.APPROVED_CALL})


@dataclass(frozen=True)
class TurnOrigin:
    """One turn's provenance, with the reference sealed where it is stored."""

    kind: OriginKind
    reference: dict[str, Any] = field(default_factory=dict)

    @property
    def may_mutate(self) -> bool:
        """Only the owner's words and an approved card may change their setup."""
        return self.kind in _MUTATING_KINDS

    def payload(self) -> dict[str, Any]:
        """What the store seals on the row: the kind and the reference."""
        return {"kind": str(self.kind), "reference": dict(self.reference)}


_UNPROMPTED = TurnOrigin(OriginKind.UNPROMPTED)

_current: ContextVar[TurnOrigin] = ContextVar("aether_turn_origin", default=_UNPROMPTED)


def current_origin() -> TurnOrigin:
    return _current.get()


def set_origin(origin: TurnOrigin) -> Any:
    """Set the origin for a long block; the caller resets the returned token.

    `use_origin` is the preferred shape for a scoped block. A turn is one
    long block that owns its own cleanup, so it takes the token directly."""
    return _current.set(origin)


def reset_origin(token: Any) -> None:
    _current.reset(token)


@contextmanager
def use_origin(origin: TurnOrigin) -> Iterator[None]:
    """Set this turn's origin for everything called inside the block."""
    token = _current.set(origin)
    try:
        yield
    finally:
        _current.reset(token)


def owner_message(reference: dict[str, Any] | None = None) -> TurnOrigin:
    return TurnOrigin(OriginKind.OWNER_MESSAGE, dict(reference or {}))


def approved_call(approval_id: int) -> TurnOrigin:
    return TurnOrigin(OriginKind.APPROVED_CALL, {"approval_id": approval_id})


def scheduled(action_id: int) -> TurnOrigin:
    return TurnOrigin(OriginKind.SCHEDULED, {"action_id": action_id})


def alert_action(alert_id: int, event_id: int) -> TurnOrigin:
    return TurnOrigin(OriginKind.ALERT_ACTION, {"alert_id": alert_id, "event_id": event_id})


def unprompted() -> TurnOrigin:
    return _UNPROMPTED


def panel_action() -> TurnOrigin:
    """A change the owner made directly in the panel — their own act."""
    return TurnOrigin(OriginKind.OWNER_MESSAGE, {"panel": True})
