"""Typed tool outcomes — what happened, in a word code can branch on.

A tool's sentence is for the person; its status is for the caller. A refusal
("the user's own words are needed"), an empty result ("nothing matched"), an
unavailable backend and a real error are four different facts, and guessing
them by matching prefixes on model-facing text is how one of them gets
mistaken for success.

Handlers may still return a plain string (treated as ``ok``) while a
capability migrates; the registry coerces every shape once. The model still
sees only the sentence: `ToolResult.content` is unchanged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

OK = "ok"
EMPTY = "empty"
REFUSED = "refused"
PENDING = "pending"
UNAVAILABLE = "unavailable"
ERROR = "error"

STATUSES = (OK, EMPTY, REFUSED, PENDING, UNAVAILABLE, ERROR)
_ERROR_STATUSES = frozenset({REFUSED, UNAVAILABLE, ERROR})


@dataclass(frozen=True)
class ToolOutcome:
    """One execution's status and the plain sentence that describes it."""

    status: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unknown tool status {self.status!r}")
        if not self.message.strip():
            raise ValueError("a tool outcome needs a message")

    @property
    def is_error(self) -> bool:
        """True for anything the model must not treat as a completed act."""
        return self.status in _ERROR_STATUSES

    @classmethod
    def ok(cls, message: str, **data: Any) -> ToolOutcome:
        return cls(OK, message, data)

    @classmethod
    def empty(cls, message: str, **data: Any) -> ToolOutcome:
        return cls(EMPTY, message, data)

    @classmethod
    def refused(cls, message: str, **data: Any) -> ToolOutcome:
        return cls(REFUSED, message, data)

    @classmethod
    def pending(cls, message: str, **data: Any) -> ToolOutcome:
        return cls(PENDING, message, data)

    @classmethod
    def unavailable(cls, message: str, **data: Any) -> ToolOutcome:
        return cls(UNAVAILABLE, message, data)

    @classmethod
    def error(cls, message: str, **data: Any) -> ToolOutcome:
        return cls(ERROR, message, data)

    @classmethod
    def coerce(cls, result: Any) -> ToolOutcome:
        """Every handler shape the registry accepts, typed once."""
        if isinstance(result, ToolOutcome):
            return result
        text = result if isinstance(result, str) else _json(result)
        return cls(OK, text)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
