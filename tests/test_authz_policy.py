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
        "create_routine",
        "list_routines",
        "set_routine_enabled",
        "delete_routine",
        "explain_decision",
        "verify_integrity",
        "get_config",
    ]:
        ruling = policy.classify(name)
        assert ruling.decision is Decision.ALLOW, name
        assert ruling.matched_rule == "builtin:internal"


def test_routine_management_beats_the_risky_verbs() -> None:
    """delete_routine and create_routine carry risky verb stems — they may
    only pass because `builtin:internal` is checked before `builtin:risky`.
    If the classifier is ever reordered, this is the test that notices."""
    policy = Policy([])
    for name in ("create_routine", "delete_routine"):
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
        {
            "tool_pattern": r"telegram__send_message",
            "decision": "allow",
            "note": "telegram is one-tap anyway",
        }
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
        {
            "tool_pattern": r"telegram__send_message",
            "param_pattern": r"(?i)\bmoney\b",
            "decision": "deny",
        }
    )
    # params containing the trigger word -> denied by the user rule
    assert (
        policy.classify("telegram__send_message", {"text": "here is the money"}).decision
        is Decision.DENY
    )
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


# -- set_config: the chat-configuration gate ---------------------------------
# _carry_out (an approved call's execution) bypasses classify, so this table
# is the only gate a config write passes. The airtight direction: tuning
# allow-lists, everything else parks.


def test_set_config_tuning_paths_are_allowed() -> None:
    policy = Policy([])
    for path in (
        "llm.provider",
        "llm.model",
        "llm.vision_model",
        "llm.salience_model",
        "llm.max_tokens",
        "llm.ollama_vision",
        "agent.tick_seconds",
        "agent.max_tool_iterations",
        "agent.daily_surface_cap",
        "agent.quiet_urgent_salience",
        "agent.quiet_hours",
        "salience.threshold",
        "salience.rate_cap_per_hour",
    ):
        ruling = policy.classify("set_config", {"op": "set", "path": path, "value": 1})
        assert ruling.decision is Decision.ALLOW, path
        assert ruling.matched_rule == "builtin:config-tune", path


def test_set_config_security_paths_require_approval() -> None:
    policy = Policy([])
    for path in (
        "messaging.telegram.enabled",
        "messaging.discord.enabled",
        "messaging.slack.enabled",
        "contacts.mode",
        "contacts.allowlist",
        "authz.rules",
        "authz.approval_ttl_hours",
        "mcp_servers",
        "mcp_servers.mail.enabled",
    ):
        ruling = policy.classify("set_config", {"op": "set", "path": path, "value": True})
        assert ruling.decision is Decision.REQUIRE_APPROVAL, path
        assert ruling.matched_rule == "builtin:config-security", path
    # the op never changes the ruling — only the path root decides
    add = policy.classify("set_config", {"op": "add", "path": "authz.rules", "value": {}})
    assert add.decision is Decision.REQUIRE_APPROVAL
    assert add.matched_rule == "builtin:config-security"


def test_set_config_unknown_or_missing_path_parks_never_allows() -> None:
    policy = Policy([])
    for params in (
        {"op": "set", "path": "totally.unknown.path", "value": 1},  # unknown root
        {"op": "set", "path": "", "value": 1},  # empty path
        {"op": "set", "value": 1},  # no path at all
        {"op": "set"},  # nothing but an op
        {},  # fully garbled
        {"op": "set", "path": "contacts", "value": 1},  # a bare security root parks too
    ):
        ruling = policy.classify("set_config", params)
        assert ruling.decision is Decision.REQUIRE_APPROVAL, params
        assert ruling.matched_rule == "builtin:config-security", params


def test_set_config_bare_tuning_root_allows_but_handler_refuses() -> None:
    """`agent` on its own is not a real path, but its root classifies as
    tuning — defense in depth is the handler: _resolve refuses it, nothing
    applies. The classifier stays root-based on purpose; this pins that the
    refusal is the ConfigManager's job, not the gate's."""
    ruling = Policy([]).classify("set_config", {"op": "set", "path": "agent", "value": 1})
    assert ruling.decision is Decision.ALLOW
    assert ruling.matched_rule == "builtin:config-tune"


def test_set_config_is_not_internal() -> None:
    """set_config must not ride the internal allow — its whole point is the
    per-path split. If it ever matched _INTERNAL, every security write would
    run silently after one tap on an unrelated card."""
    ruling = Policy([]).classify("set_config", {"op": "set", "path": "authz.rules", "value": []})
    assert ruling.decision is Decision.REQUIRE_APPROVAL
    assert ruling.matched_rule == "builtin:config-security"


def test_user_rules_beat_the_set_config_branch_both_directions() -> None:
    # allow: the user pre-trusted a security-shaped path with one of their own
    # rules — their rule, their standing trust
    trusts = _policy(
        {"tool_pattern": r"set_config", "param_pattern": r"messaging\.", "decision": "allow"}
    )
    ruling = trusts.classify(
        "set_config", {"op": "set", "path": "messaging.telegram.enabled", "value": True}
    )
    assert ruling.decision is Decision.ALLOW
    assert ruling.matched_rule == "user:set_config"
    # deny: the user locks down a tuning path the builtin would allow
    locks = _policy({"tool_pattern": r"set_config", "decision": "deny"})
    ruling = locks.classify("set_config", {"op": "set", "path": "llm.model", "value": "x"})
    assert ruling.decision is Decision.DENY
    assert ruling.matched_rule == "user:set_config"


# -- origin: a connect the user just drove in chat --------------------------------


def test_a_walk_or_oauth_origin_allows_a_connect_directly() -> None:
    """The /apps pick-and-paste (origin "walk") and a sign-in completing at
    the callback (origin "oauth") are the user's own approval — the connect
    applies instead of parking, and the audit says which one it was."""
    policy = Policy([])
    for origin, rule in (("walk", "builtin:walk-connect"), ("oauth", "builtin:oauth-consent")):
        add = policy.classify(
            "set_config",
            {"op": "add", "path": "mcp_servers", "value": {"name": "github"}},
            origin=origin,
        )
        assert add.decision is Decision.ALLOW, origin
        assert add.matched_rule == rule, origin
        toggle = policy.classify(
            "set_config",
            {"op": "set", "path": "messaging.telegram.enabled", "value": True},
            origin=origin,
        )
        assert toggle.decision is Decision.ALLOW, origin
        assert toggle.matched_rule == rule, origin


def test_only_the_connect_shaped_roots_ride_an_origin() -> None:
    """contacts and authz park under any origin — an in-chat walk must never
    become a way to widen who Aether listens to."""
    policy = Policy([])
    for origin in ("walk", "oauth"):
        for path in ("contacts.allowlist", "authz.rules"):
            ruling = policy.classify(
                "set_config", {"op": "set", "path": path, "value": True}, origin=origin
            )
            assert ruling.decision is Decision.REQUIRE_APPROVAL, (origin, path)
            assert ruling.matched_rule == "builtin:config-security", (origin, path)


def test_no_or_unrecognized_origin_keeps_todays_rules_exactly() -> None:
    """The origin argument is loop-internal — a model proposal can never
    produce it, and its absence (or anything unrecognized) must classify
    exactly as before."""
    policy = Policy([])
    for origin in (None, "model", ""):
        ruling = policy.classify(
            "set_config", {"op": "add", "path": "mcp_servers", "value": {}}, origin=origin
        )
        assert ruling.decision is Decision.REQUIRE_APPROVAL, origin
        assert ruling.matched_rule == "builtin:config-security", origin


def test_user_rules_still_beat_the_origin_branch() -> None:
    """A user rule that forces a hold or a deny wins over the walk's direct
    apply — their rule, their standing decision; the walk relays the card."""
    holds = _policy(
        {"tool_pattern": r"set_config", "param_pattern": r"mcp_servers", "decision": "approve"}
    )
    ruling = holds.classify(
        "set_config", {"op": "add", "path": "mcp_servers", "value": {}}, origin="walk"
    )
    assert ruling.decision is Decision.REQUIRE_APPROVAL
    assert ruling.matched_rule == "user:set_config"
    denies = _policy({"tool_pattern": r"set_config", "decision": "deny"})
    ruling = denies.classify(
        "set_config", {"op": "add", "path": "mcp_servers", "value": {}}, origin="oauth"
    )
    assert ruling.decision is Decision.DENY
    assert ruling.matched_rule == "user:set_config"


def test_origin_never_touches_any_other_tool() -> None:
    """Only set_config reads origin — a walk in progress must never make a
    risky verb any less risky."""
    policy = Policy([])
    ruling = policy.classify("send_message", {"text": "hi"}, origin="walk")
    assert ruling.decision is Decision.REQUIRE_APPROVAL
    assert ruling.matched_rule == "builtin:risky"
