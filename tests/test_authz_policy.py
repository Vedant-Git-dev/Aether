"""Policy classifier tests — pure functions, the whole decision table."""

import pytest

from aether.authz.policy import Decision, Policy, PolicyError, params_blob
from aether.config import AuthzRule


def _policy(*rules: dict) -> Policy:
    return Policy([AuthzRule(**r) for r in rules])


def test_read_only_tools_are_allowed() -> None:
    policy = Policy([])
    for name in [
        "mail__list_unread",
        "calendar__get_events",
        "web__fetch_page",
        "slack__find_messages",
        "telegram__get_me",
        "gmail__search_emails",
        "notion__query_database",
    ]:
        ruling = policy.classify(name)
        assert ruling.decision is Decision.ALLOW, name
        assert ruling.matched_rule == "builtin:read-only"


def test_internal_tools_are_allowed() -> None:
    policy = Policy([])
    for name in [
        "memory_search",
        "memory_write",
        "note_entity",
        "schedule_action",
        "request_screen_capture",
        "get_pending_approvals",
        "send_chat_message",
    ]:
        ruling = policy.classify(name)
        assert ruling.decision is Decision.ALLOW, name
        assert ruling.matched_rule == "builtin:internal"


def test_risky_tools_require_approval() -> None:
    policy = Policy([])
    for name in [
        "telegram__send_message",
        "mail__send_email",
        "mail__reply_to",
        "mail__delete_all",
        "payments__pay_invoice",
        "calendar__create_event",
        "mail__book_flight",
        "slack__post_message",
        "github__transfer_repo",
        "zoom__invite_user",
        "twitter__publish_tweet",
        "hotel__cancel_booking",
    ]:
        ruling = policy.classify(name)
        assert ruling.decision is Decision.REQUIRE_APPROVAL, name
        assert ruling.matched_rule == "builtin:risky"


def test_unknown_tools_fail_safe_to_approval() -> None:
    ruling = Policy([]).classify("weird__totally_new_thing")
    assert ruling.decision is Decision.REQUIRE_APPROVAL
    assert ruling.matched_rule == "default:fail-safe"


def test_risky_beats_read_only_when_both_match() -> None:
    # "fetch" would allow, but the delete wins — the risky check runs first.
    ruling = Policy([]).classify("mail__fetch_and_delete")
    assert ruling.decision is Decision.REQUIRE_APPROVAL


def test_verb_must_start_a_word() -> None:
    # "resend" and "facebook" contain "send"/"book" inside a word — neither
    # trips the risky rule; unknowns fall through to the fail-safe default.
    assert Policy([]).classify("mail__resend").matched_rule == "default:fail-safe"
    # ...while "facebook__list_events" is a read-only tool, not a booking one
    ruling = Policy([]).classify("facebook__list_events")
    assert ruling.decision is Decision.ALLOW
    assert ruling.matched_rule == "builtin:read-only"


def test_user_allow_rule_overrides_builtin_risky() -> None:
    policy = _policy(
        {"tool_pattern": r"telegram__send_message", "decision": "allow", "note": "telegram is one-tap anyway"}
    )
    ruling = policy.classify("telegram__send_message", {"chat_id": 1})
    assert ruling.decision is Decision.ALLOW
    assert ruling.matched_rule == "user:telegram__send_message"
    assert ruling.reason == "telegram is one-tap anyway"


def test_user_deny_rule() -> None:
    policy = _policy({"tool_pattern": r"payments__.*", "decision": "deny"})
    assert Policy([]).classify("payments__pay_invoice").decision is Decision.REQUIRE_APPROVAL
    ruling = policy.classify("payments__pay_invoice")
    assert ruling.decision is Decision.DENY
    assert ruling.matched_rule == "user:payments__.*"


def test_user_param_pattern_gates_the_rule() -> None:
    policy = _policy(
        {"tool_pattern": r"telegram__send_message", "param_pattern": r"(?i)\bmoney\b", "decision": "deny"}
    )
    # params containing the trigger word -> denied by the user rule
    assert policy.classify("telegram__send_message", {"text": "here is the money"}).decision is Decision.DENY
    # params without it -> rule doesn't apply, builtin risky takes over
    clean = policy.classify("telegram__send_message", {"text": "hello!"})
    assert clean.decision is Decision.REQUIRE_APPROVAL
    assert clean.matched_rule == "builtin:risky"


def test_first_matching_user_rule_wins() -> None:
    policy = _policy(
        {"tool_pattern": r"mail__send.*", "param_pattern": r"(?i)urgent", "decision": "deny"},
        {"tool_pattern": r"mail__send.*", "decision": "allow"},
    )
    assert policy.classify("mail__send_email", {"subject": "urgent fix"}).decision is Decision.DENY
    assert policy.classify("mail__send_email", {"subject": "hi"}).decision is Decision.ALLOW


def test_bad_regex_raises_policy_error() -> None:
    with pytest.raises(PolicyError, match="bad regex"):
        Policy([AuthzRule(tool_pattern="([", decision="allow")])


def test_params_blob_is_canonical() -> None:
    # key order must not change the blob a param regex sees
    assert params_blob({"a": 1, "b": 2}) == params_blob({"b": 2, "a": 1})
    assert params_blob({"text": "send Money now"}) == '{"text":"send Money now"}'
