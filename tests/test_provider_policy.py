"""Provider effects are explicit, and a user rule still wins."""

from __future__ import annotations

from aether.authz.policy import Decision, Policy
from aether.config import AuthzRule


def test_provider_read_runs() -> None:
    ruling = Policy([]).classify("gmail__list_messages", effect="read")
    assert ruling.decision is Decision.ALLOW
    assert ruling.matched_rule == "builtin:provider-read"


def test_provider_write_parks() -> None:
    ruling = Policy([]).classify("gmail__send_email", effect="write")
    assert ruling.decision is Decision.REQUIRE_APPROVAL
    assert ruling.matched_rule == "builtin:provider-write"


def test_provider_destructive_parks() -> None:
    ruling = Policy([]).classify("github__delete_repo", effect="destructive")
    assert ruling.decision is Decision.REQUIRE_APPROVAL
    assert ruling.matched_rule == "builtin:provider-destructive"


def test_a_user_rule_beats_the_effect() -> None:
    policy = Policy(
        [
            AuthzRule(
                tool_pattern=r"^gmail__send_email$",
                decision="allow",
                note="owner trusts their own mail",
            )
        ]
    )
    ruling = policy.classify("gmail__send_email", effect="write")
    assert ruling.decision is Decision.ALLOW
    assert ruling.matched_rule == "user:^gmail__send_email$"


def test_unknown_effect_still_fails_safe() -> None:
    ruling = Policy([]).classify("gmail__send_email", effect="mystery")
    assert ruling.decision is Decision.REQUIRE_APPROVAL
    assert ruling.matched_rule == "builtin:risky"


def test_providers_section_is_personal_tuning() -> None:
    ruling = Policy([]).classify(
        "set_config", {"op": "set", "path": "providers.gmail.poll_minutes"}
    )
    assert ruling.decision is Decision.ALLOW
    assert ruling.matched_rule == "builtin:config-tune"
