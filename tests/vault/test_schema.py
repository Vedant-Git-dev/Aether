from __future__ import annotations

import copy

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from aether.message_catalog import catalog
from aether.vault import schema
from aether.vault.errors import (
    DENIAL_REASONS,
    ERROR_CODES,
    VAULT_CATALOG_KEYS,
    VaultProtocolError,
)
from aether.vault.policy import policy

B16 = schema.b64u_encode(bytes(range(16)))
B32 = schema.b64u_encode(bytes(range(32)))
PHONE_PUB = (
    ec.derive_private_key(1, ec.SECP256R1())
    .public_key()
    .public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
)


def common(kind: str, epoch: int = 1) -> dict:
    return {
        "v": 1,
        "type": kind,
        "vault": B16,
        "device": schema.b64u_encode(bytes(range(16, 32))),
        "epoch": epoch,
    }


def sample_manifest(revision: int = 1, previous: str | None = None) -> dict:
    entries = [
        {
            "cat": cat,
            "ver": 1,
            "status": "active",
            "wrap": {"alg": "android-keystore-aes-gcm", "blob": schema.b64u_encode(b"\x00" * 28)},
            "backup": {"enc": B32, "ct": schema.b64u_encode(b"\x01" * 16)},
        }
        for cat in schema.CATEGORIES
    ]
    return {
        **common("manifest"),
        "revision": revision,
        "previous_digest": previous,
        "ingress_public": [{"ver": 1, "public": B32}],
        "recovery_public": B32,
        "recovery_verify": B32,
        "entries": entries,
    }


def samples() -> dict[str, dict]:
    return {
        "pair": {
            **common("pair"),
            "origin": "https://vault.example.test",
            "server_kid": B32,
            "phone_pub": schema.b64u_encode(PHONE_PUB),
            "manifest_digest": B32,
            "nonce": B32,
            "iat": 1_700_000_000,
        },
        "device_session": {**common("device_session"), "challenge": B32, "iat": 1_700_000_000},
        "panel_consent": {
            **common("panel_consent"),
            "intent": B16,
            "challenge_hash": B32,
            "iat": 1_700_000_000,
        },
        "control": {
            **common("control"),
            "op": "lock",
            "next_epoch": 2,
            "challenge": B32,
            "iat": 1_700_000_000,
        },
        "manifest": sample_manifest(),
        "push_registration": {
            **common("push_registration"),
            "challenge": B32,
            "provider": "fcm",
            "registration": B32,
            "iat": 1_700_000_000,
        },
        "lease_request": {
            **common("lease_request", epoch=2),
            "boot": B16,
            "task": schema.b64u_encode(bytes(range(32, 48))),
            "operation": "turn",
            "use_case": "turn",
            "request": schema.b64u_encode(bytes(range(48, 64))),
            "nonce": B32,
            "iat": 1_700_000_000,
            "accept_until": 1_700_000_030,
            "refs": [{"cat": "memory", "ver": 1}],
            "ttl": 60,
            "epk": B32,
            "manifest_digest": B32,
        },
        "lease_grant": {
            **common("lease_grant", epoch=2),
            "boot": B16,
            "task": schema.b64u_encode(bytes(range(32, 48))),
            "request": schema.b64u_encode(bytes(range(48, 64))),
            "nonce": B32,
            "request_digest": B32,
            "lease": schema.b64u_encode(bytes(range(64, 80))),
            "iat": 1_700_000_000,
            "exp": 1_700_000_060,
            "refs": [{"cat": "memory", "ver": 1}],
            "manifest_digest": B32,
            "box": {"enc": B32, "ct": schema.b64u_encode(b"\x02" * 32)},
        },
        "lease_denied": {
            **common("lease_denied", epoch=2),
            "boot": B16,
            "task": schema.b64u_encode(bytes(range(32, 48))),
            "request": schema.b64u_encode(bytes(range(48, 64))),
            "nonce": B32,
            "request_digest": B32,
            "iat": 1_700_000_000,
            "reason": "locked",
        },
        "restore": {
            **common("restore", epoch=7),
            "challenge": B32,
            "previous_digest": B32,
            "new_manifest_digest": B32,
            "new_phone_pub": schema.b64u_encode(PHONE_PUB),
            "iat": 1_700_000_000,
        },
    }


@pytest.mark.parametrize("kind", schema.MESSAGE_TYPES)
def test_every_message_type_has_a_valid_sample(kind: str) -> None:
    schema.validate_body(samples()[kind], expected_type=kind)


def test_expected_type_is_enforced() -> None:
    with pytest.raises(VaultProtocolError) as caught:
        schema.validate_body(samples()["pair"], expected_type="manifest")
    assert caught.value.code == "bad_field"


def test_version_is_closed() -> None:
    body = copy.deepcopy(samples()["pair"])
    body["v"] = 2
    with pytest.raises(VaultProtocolError) as caught:
        schema.validate_body(body)
    assert caught.value.code == "bad_version"


def test_unknown_and_missing_fields_are_rejected() -> None:
    extra = copy.deepcopy(samples()["device_session"])
    extra["surprise"] = 1
    with pytest.raises(VaultProtocolError) as caught:
        schema.validate_body(extra)
    assert caught.value.code == "bad_field"

    missing = copy.deepcopy(samples()["device_session"])
    del missing["challenge"]
    with pytest.raises(VaultProtocolError) as caught:
        schema.validate_body(missing)
    assert caught.value.code == "bad_field"


@pytest.mark.parametrize("padded", ["YQ==", "YQ=", "A"])
def test_base64url_must_be_unpadded_and_canonical(padded: str) -> None:
    with pytest.raises(VaultProtocolError) as caught:
        schema.b64u_decode(padded, field="x")
    assert caught.value.code == "bad_field"


def test_strict_json_rejects_duplicates_floats_and_trailing_bytes() -> None:
    with pytest.raises(VaultProtocolError) as caught:
        schema.loads_strict('{"a":1,"a":2}')
    assert caught.value.code == "duplicate_field"

    with pytest.raises(VaultProtocolError) as caught:
        schema.loads_strict('{"a":1.5}')
    assert caught.value.code == "bad_field"

    with pytest.raises(VaultProtocolError) as caught:
        schema.loads_strict('{"a":NaN}')
    assert caught.value.code == "bad_field"

    with pytest.raises(VaultProtocolError) as caught:
        schema.loads_strict('{"a":1} trailing')
    assert caught.value.code == "malformed"


def test_origin_normalization() -> None:
    assert schema.normalize_origin("https://Vault.Example.Test:443") == "https://vault.example.test"
    assert (
        schema.normalize_origin("https://vault.example.test:8443")
        == "https://vault.example.test:8443"
    )
    for bad in (
        "http://vault.example.test",
        "https://user:pass@vault.example.test",
        "https://vault.example.test/path",
        "https://vault.example.test?x=1",
        "https://vault.example.test#x",
        "https://vault.example.test?",
        "https://vault.example.test/",
        "https://vault.example.test:bad",
        "https://exa mple.test",
    ):
        with pytest.raises(VaultProtocolError):
            schema.normalize_origin(bad)


def test_refs_are_sorted_unique_and_known() -> None:
    assert schema.validate_refs([{"cat": "memory", "ver": 1}]) == [("memory", 1)]
    with pytest.raises(VaultProtocolError):
        schema.validate_refs([{"cat": "memory", "ver": 1}, {"cat": "memory", "ver": 1}])
    with pytest.raises(VaultProtocolError):
        schema.validate_refs([{"cat": "workspace", "ver": 1}, {"cat": "memory", "ver": 1}])
    with pytest.raises(VaultProtocolError):
        schema.validate_refs([{"cat": "unknown", "ver": 1}])


def test_use_case_registry_is_closed_and_matches_operation() -> None:
    assert schema.validate_use_case("turn", "turn") == schema.OPERATION_CATEGORIES["turn"]
    with pytest.raises(VaultProtocolError):
        schema.validate_use_case("turn", "drain")
    with pytest.raises(VaultProtocolError):
        schema.validate_use_case("not-a-use-case", "turn")


def test_refs_cannot_exceed_the_operation() -> None:
    body = copy.deepcopy(samples()["lease_request"])
    body["refs"] = [{"cat": "credentials", "ver": 1}]
    with pytest.raises(VaultProtocolError) as caught:
        schema.validate_body(body)
    assert caught.value.code == "bad_scope"


def test_lease_window_and_ttl_bounds() -> None:
    body = copy.deepcopy(samples()["lease_request"])
    body["accept_until"] += 1
    with pytest.raises(VaultProtocolError):
        schema.validate_body(body)

    body = copy.deepcopy(samples()["lease_request"])
    body["ttl"] = 0
    with pytest.raises(VaultProtocolError):
        schema.validate_body(body)

    body = copy.deepcopy(samples()["lease_request"])
    body["ttl"] = 301
    with pytest.raises(VaultProtocolError):
        schema.validate_body(body)


def test_control_advances_exactly_one_epoch() -> None:
    body = copy.deepcopy(samples()["control"])
    body["next_epoch"] = 3
    with pytest.raises(VaultProtocolError):
        schema.validate_body(body)


def test_manifest_rules() -> None:
    body = copy.deepcopy(samples()["manifest"])
    body["entries"][0]["status"] = "active"
    body["entries"][1]["status"] = "active"
    body["entries"][1]["cat"] = body["entries"][0]["cat"]
    with pytest.raises(VaultProtocolError):
        schema.validate_body(body)

    body = copy.deepcopy(samples()["manifest"])
    body["entries"][0], body["entries"][1] = body["entries"][1], body["entries"][0]
    with pytest.raises(VaultProtocolError):
        schema.validate_body(body)

    body = copy.deepcopy(samples()["manifest"])
    body["ingress_public"][0]["ver"] = 2
    with pytest.raises(VaultProtocolError):
        schema.validate_body(body)

    body = copy.deepcopy(samples()["manifest"])
    body["previous_digest"] = B32
    with pytest.raises(VaultProtocolError):
        schema.validate_body(body)


def test_grant_lifetime_is_bounded() -> None:
    body = copy.deepcopy(samples()["lease_grant"])
    body["exp"] = body["iat"]
    with pytest.raises(VaultProtocolError):
        schema.validate_body(body)

    body = copy.deepcopy(samples()["lease_grant"])
    body["exp"] = body["iat"] + 301
    with pytest.raises(VaultProtocolError):
        schema.validate_body(body)


def test_denial_reason_is_closed() -> None:
    body = copy.deepcopy(samples()["lease_denied"])
    body["reason"] = "because"
    with pytest.raises(VaultProtocolError):
        schema.validate_body(body)


def test_restore_digests_are_full_length() -> None:
    body = copy.deepcopy(samples()["restore"])
    body["previous_digest"] = schema.b64u_encode(b"\x00" * 16)
    with pytest.raises(VaultProtocolError):
        schema.validate_body(body)


def test_error_payload_is_a_closed_catalog() -> None:
    for code in ERROR_CODES:
        assert schema.validate_error_payload({"code": code}) == {"code": code}
    with pytest.raises(VaultProtocolError):
        schema.validate_error_payload({"code": "not-a-code"})
    with pytest.raises(VaultProtocolError):
        schema.validate_error_payload({"code": "locked", "detail": "x"})


def test_status_payload_is_closed() -> None:
    assert schema.validate_status_payload({"state": "locked", "epoch": 2}) == {
        "state": "locked",
        "epoch": 2,
    }
    with pytest.raises(VaultProtocolError):
        schema.validate_status_payload({"state": "ready", "epoch": 2})


def test_every_vault_catalog_key_exists() -> None:
    message_catalog = catalog()
    missing = sorted(key for key in VAULT_CATALOG_KEYS if not message_catalog.has(key))
    assert missing == []


def test_denial_reasons_and_status_have_catalog_keys() -> None:
    message_catalog = catalog()
    assert all(message_catalog.has(f"vault.denied.{reason}") for reason in DENIAL_REASONS)
    assert all(
        message_catalog.has(f"vault.status.{state}")
        for state in ("available", "locked", "offline", "delayed")
    )


def test_paths_are_fixed_and_unique() -> None:
    paths = list(schema.PATHS.values())
    assert len(paths) == len(set(paths))
    for name, path in schema.PATHS.items():
        assert path.startswith("/")
        assert "?" not in path and "#" not in path
        assert schema.vault_path(name) == schema.API_PREFIX + path
    with pytest.raises(VaultProtocolError):
        schema.vault_path("missing")


def test_policy_defaults_are_the_protocol_values() -> None:
    conf = policy()
    assert conf.v == 1
    assert conf.accept_window_seconds == 30
    assert conf.clock_skew_seconds == 15
    assert conf.grant_ceiling_seconds == 300
    assert conf.grant_ttl_min_seconds == 1
    assert conf.grant_ttl_max_seconds == 300
    assert conf.challenge_seconds == 60
    assert conf.device_session_seconds == 900
    assert conf.pair_ticket_seconds == 300
    assert conf.panel_intent_seconds == 120
    assert conf.ws_ticket_seconds == 30
    assert conf.rate_per_minute == 30
    assert conf.rate_burst == 6
    assert conf.nonce_ledger_seconds == 45
    assert conf.aes_gcm_budget == 1 << 32
    assert conf.aes_gcm_operational_threshold < conf.aes_gcm_budget


def test_unapproved_use_cases_fail_closed(monkeypatch) -> None:
    monkeypatch.setattr(schema, "USE_CASES", {})
    with pytest.raises(VaultProtocolError, match="use_case is not registered"):
        schema.validate_use_case("turn", "turn")
