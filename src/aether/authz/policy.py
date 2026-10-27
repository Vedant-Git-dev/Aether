"""Deterministic authorization policy — no LLM in the decision path.

Every tool call the model proposes is classified by code and declared rules
before it executes. First match wins:

1. user rules from config.yaml — explicit trust or distrust, a regex on the
   tool name, optionally combined with a regex on the call parameters
2. set_config — personal tuning (agent.*, salience.*, llm.*, and the
   background-work sections proactive.*/workbench.* — the user's own
   budgets and switches, with the worst case a capped retry) -> ALLOW;
   a connect the user just drove in chat (origin "walk" from the /apps
   pick-and-paste — a loop-internal argument the model can never produce)
   on mcp_servers or messaging -> ALLOW, because the user's pick was the
   approval; every other config path (the security sections, an unknown or
   missing one, contacts/authz under any origin) -> REQUIRE_APPROVAL
3. internal Aether tools (memory, scheduling, capture requests) -> ALLOW
4. Aether's web research (the hardened fetch, the key-gated search) ->
   ALLOW — read-only by construction, external by destination
5. Aether's image generation -> ALLOW — the prompt reaches the image
   provider (a stated, capped channel), but the result is a stored
   file; sending it anywhere still waits for approval
6. risky verbs (send, delete, pay, ...) -> REQUIRE_APPROVAL
7. read-only verbs (list, search, get, ...) -> ALLOW
8. anything else -> REQUIRE_APPROVAL

Direct provider tools carry an explicit effect (read, write, destructive).
The effect is checked right after user rules, before the verb ladders: a
provider's own declaration is more precise than a name, and a missing or
unknown effect still falls through to the fail-safe. Provider reads run;
provider writes and destructive actions park.

The verb ladders (5 and 6) run against the lowercased tool name: Composio
names its tools UPPER_SNAKE (composio__GMAIL_SEND_EMAIL), and a verb is a
verb in either case — without it every hub tool would park at fail-safe.
A composio tool's name is TOOLKIT_ACTION (gmail is metadata, send_email is
the act), so the hub's tools are classified on the action part alone.

The fail-safe direction matters: an unknown tool can never run silently —
the worst case is an extra approval tap, never an unreviewed action.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..config import AuthzRule


class Decision(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass(frozen=True)
class Ruling:
    decision: Decision
    matched_rule: str  # "user:<pattern>" | "builtin:walk-connect" | "builtin:config-tune" | "builtin:config-security" | "builtin:provider-read" | "builtin:provider-write" | "builtin:provider-destructive" | "builtin:internal" | "builtin:web-research" | "builtin:media-generation" | "builtin:risky" | "builtin:read-only" | "default:fail-safe"
    reason: str


# Internal tools read and write Aether's own state only; they never reach an
# external system under their own power. send_chat_message and share_media
# are included because they talk solely to the owner's own chat surfaces —
# sharing a stored file back to the owner is the same trust line as any chat
# reply, and anything further out (an email, another person) is a different
# call that parks; verify_integrity because it only reads Aether's own audit
# chain back to the owner, the routine tools because they manage Aether's
# own stored instructions (delete_routine removes a stored trigger, not
# anything external), and the workspace tools because they only read/write
# Aether's own Markdown workspace on disk. edit_video writes only a new row
# in Aether's own encrypted store from a sandbox job under the media
# budget — the file goes nowhere until a separate act sends it. Checked
# before the risky verbs, which is what lets "delete_routine" and
# "workspace_remove" (if ever added) win over the bare "delete"/"remove".
_INTERNAL = re.compile(
    r"^(?!.*__)(memory_\w+|note_entity|schedule_action|request_screen_capture"
    r"|look_at_media|share_media|edit_video|get_pending_approvals|send_chat_message"
    r"|create_routine|list_routines"
    r"|set_routine_enabled|delete_routine|explain_decision|verify_integrity"
    r"|get_config|workspace_\w+)$"
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

# Aether's own web research — the hardened fetch and the key-gated search.
# They reach the public web, so they are not internal, but they only read:
# the fetcher's address rules and byte caps make it observation, not
# action. Explicit rather than riding the verb ladder, because the names
# start with "web_" and the read-only verb anchor would miss them.
_WEB_RESEARCH = re.compile(r"^web_(fetch|search)$")

# Aether's image generation. Keyed or keyless, the call is external —
# the prompt reaches the image provider, a stated and capped channel —
# but the output is a stored file, not an act: the image goes nowhere
# until a separate send parks or shares it. Explicit rather than riding
# the verb ladder because "generate" is neither risky nor read-only,
# and the fail-safe default would park every attempt.
_MEDIA_GENERATION = re.compile(r"^generate_image$")


# The config vocabulary's two faces, by path root. Personal tuning applies
# right away; the security sections change who Aether listens to, what it
# may do, or what it's connected to — those park. Anything that isn't a
# recognized tuning root parks too: an unrecognized config path can never
# apply silently. The airtight direction matters because an approved call
# is carried out without re-classifying — park-time is the only gate a
# security write passes, and the worst case is an extra approval tap.
_CONFIG_TUNING_ROOTS = {"agent", "salience", "llm", "proactive", "workbench", "providers"}
_CONFIG_SECURITY_ROOTS = {"contacts", "authz", "messaging", "mcp_servers"}


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
                raise PolicyError(f"bad regex in authz rule {rule.tool_pattern!r}: {exc}") from exc
            self._rules.append((tool_re, param_re, _DECISIONS[rule.decision], rule.note))

    def classify(
        self,
        tool_name: str,
        params: dict[str, Any] | None = None,
        origin: str | None = None,
        effect: str | None = None,
    ) -> Ruling:
        params = params or {}
        for tool_re, param_re, decision, note in self._rules:
            if tool_re.search(tool_name) and (
                param_re is None or param_re.search(params_blob(params))
            ):
                return Ruling(
                    decision,
                    f"user:{tool_re.pattern}",
                    note or f"user rule {tool_re.pattern!r}",
                )
        if tool_name == "set_config":
            connect = self._classify_connect(params, origin)
            if connect is not None:
                return connect
            return self._classify_set_config(params)
        if effect == "read":
            return Ruling(
                Decision.ALLOW,
                "builtin:provider-read",
                "a read from a connected app — nothing is changed",
            )
        if effect in ("write", "destructive"):
            return Ruling(
                Decision.REQUIRE_APPROVAL,
                "builtin:provider-write" if effect == "write" else "builtin:provider-destructive",
                "changes something in a connected app — the user decides that with one tap",
            )
        if _INTERNAL.match(tool_name):
            return Ruling(
                Decision.ALLOW, "builtin:internal", "internal tool, touches only Aether's own state"
            )
        if _WEB_RESEARCH.match(tool_name):
            return Ruling(
                Decision.ALLOW,
                "builtin:web-research",
                "web research — reads a page or search results, nothing is changed",
            )
        if _MEDIA_GENERATION.match(tool_name):
            return Ruling(
                Decision.ALLOW,
                "builtin:media-generation",
                "image generation — the prompt reaches the provider and the "
                "result is a stored file; sending it anywhere still waits for approval",
            )
        lowered = tool_name.lower()  # Composio's UPPER_SNAKE names classify by the same verbs
        if lowered.startswith("composio__"):
            # composio names are TOOLKIT_ACTION — the toolkit segment is
            # metadata; classify on the action alone (gmail_fetch_emails
            # reads as a fetch, gmail_send_email as a send)
            action = lowered.split("__", 1)[1]
            lowered = action.split("_", 1)[-1] if "_" in action else action
        if _RISKY.search(lowered):
            return Ruling(
                Decision.REQUIRE_APPROVAL,
                "builtin:risky",
                "reaches an external system or is hard to undo",
            )
        if _READONLY.search(lowered):
            return Ruling(Decision.ALLOW, "builtin:read-only", "read-only tool, no side effects")
        return Ruling(
            Decision.REQUIRE_APPROVAL,
            "default:fail-safe",
            "unknown tool — held for a human decision",
        )

    @staticmethod
    def _classify_connect(params: dict[str, Any], origin: str | None) -> Ruling | None:
        """A connect the user just drove in chat — the /apps walk's
        pick-and-paste — applies directly, because the pick was the
        approval. `origin` is a loop-internal argument the model can never
        produce, and only the connect-shaped roots can ride it: contacts
        and authz park under any origin, and an absent or unrecognized
        origin keeps today's rules exactly."""
        if origin != "walk":
            return None
        root = str(params.get("path", "")).strip().partition(".")[0].lower()
        if root not in ("mcp_servers", "messaging"):
            return None
        return Ruling(
            Decision.ALLOW,
            "builtin:walk-connect",
            "an app connect the user just drove in chat — the pick-and-paste is the approval",
        )

    @staticmethod
    def _classify_set_config(params: dict[str, Any]) -> Ruling:
        """The config gate. The path root decides: tuning applies right
        away, everything else — the security sections, an unknown, garbled,
        or missing path — parks. A bad op on a tuning path is refused by the
        handler and never applies, but defense in depth still means only the
        tuning roots can classify ALLOW here."""
        root = str(params.get("path", "")).strip().partition(".")[0].lower()
        if root in _CONFIG_TUNING_ROOTS:
            return Ruling(
                Decision.ALLOW,
                "builtin:config-tune",
                "personal tuning — applies right away",
            )
        if root in _CONFIG_SECURITY_ROOTS:
            return Ruling(
                Decision.REQUIRE_APPROVAL,
                "builtin:config-security",
                "changes who Aether listens to, what it may do, or what "
                "it's connected to — the user decides that with one tap",
            )
        return Ruling(
            Decision.REQUIRE_APPROVAL,
            "builtin:config-security",
            "an unrecognized config path can never apply silently — held for the user to look at",
        )
