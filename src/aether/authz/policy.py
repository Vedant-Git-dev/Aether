"""Deterministic authorization policy — no LLM in the decision path.

Every tool call the model proposes is classified by code and declared rules
before it executes. First match wins:

1. user rules from config.yaml — explicit trust or distrust, a regex on the
   tool name, optionally combined with a regex on the call parameters
2. internal Aether tools (memory, scheduling, capture requests) -> ALLOW
3. risky verbs (send, delete, pay, ...) -> REQUIRE_APPROVAL
4. read-only verbs (list, search, get, ...) -> ALLOW
5. anything else -> REQUIRE_APPROVAL

The fail-safe direction matters: an unknown tool can never run silently —
the worst case is an extra approval tap, never an unreviewed action.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..config import AuthzRule


class Decision(str, Enum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass(frozen=True)
class Ruling:
    decision: Decision
    matched_rule: str  # "user:<pattern>" | "builtin:internal" | "builtin:risky" | "builtin:read-only" | "default:fail-safe"
    reason: str


# Internal tools read and write Aether's own state only; they never reach an
# external system under their own power. send_chat_message is included
# because it talks solely to the owner's own chat surfaces, and the routine
# tools because they manage Aether's own stored instructions (delete_routine
# removes a stored trigger, not anything external) — checked before the risky
# verbs, which is what lets "delete_routine" win over the bare "delete".
_INTERNAL = re.compile(
    r"^(memory_\w+|note_entity|schedule_action|request_screen_capture"
    r"|get_pending_approvals|send_chat_message|create_routine|list_routines"
    r"|set_routine_enabled|delete_routine)$"
)

# Verbs that reach an external system or are hard to undo. The verb must
# start a word inside the name ("fetch_and_delete" is risky, "resend" and
# "facebook" are not), and risky is checked before read-only so a compound
# name can never sneak a destructive verb past an observation verb.
_RISKY = re.compile(
    r"(^|__|_)(send|reply|forward|post|publish|delete|remove|cancel|pay"
    r"|transfer|invite|book|create)([a-z_]|$)"
)

# Read-only observation. Anchored to the start of the tool's own segment,
# so "getter_sync" reads as a get, but "and_get" inside a compound name
# never grants read-only status on its own.
_READONLY = re.compile(r"(^|__)(list|search|get|read|fetch|find|query)([a-z_]|$)")


class PolicyError(ValueError):
    """A configured rule is unusable (bad regex)."""


def params_blob(params: dict[str, Any]) -> str:
    """Stable serialization of call parameters that a param regex runs
    against — same canonical form the audit digest uses."""
    return json.dumps(
        params, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


# config vocabulary ("approve") -> internal decision
_DECISIONS = {
    "allow": Decision.ALLOW,
    "approve": Decision.REQUIRE_APPROVAL,
    "deny": Decision.DENY,
}


class Policy:
    """Precompiled rule set; `classify` is a pure function of its inputs."""

    def __init__(self, rules: list[AuthzRule]) -> None:
        self._rules: list[tuple[re.Pattern[str], re.Pattern[str] | None, Decision, str]] = []
        for rule in rules:
            try:
                tool_re = re.compile(rule.tool_pattern)
                param_re = re.compile(rule.param_pattern) if rule.param_pattern else None
            except re.error as exc:
                raise PolicyError(
                    f"bad regex in authz rule {rule.tool_pattern!r}: {exc}"
                ) from exc
            self._rules.append(
                (tool_re, param_re, _DECISIONS[rule.decision], rule.note)
            )

    def classify(self, tool_name: str, params: dict[str, Any] | None = None) -> Ruling:
        params = params or {}
        for tool_re, param_re, decision, note in self._rules:
            if tool_re.search(tool_name):
                if param_re is None or param_re.search(params_blob(params)):
                    return Ruling(
                        decision,
                        f"user:{tool_re.pattern}",
                        note or f"user rule {tool_re.pattern!r}",
                    )
        if _INTERNAL.match(tool_name):
            return Ruling(
                Decision.ALLOW, "builtin:internal", "internal tool, touches only Aether's own state"
            )
        if _RISKY.search(tool_name):
            return Ruling(
                Decision.REQUIRE_APPROVAL, "builtin:risky", "reaches an external system or is hard to undo"
            )
        if _READONLY.search(tool_name):
            return Ruling(Decision.ALLOW, "builtin:read-only", "read-only tool, no side effects")
        return Ruling(
            Decision.REQUIRE_APPROVAL, "default:fail-safe", "unknown tool — held for a human decision"
        )
