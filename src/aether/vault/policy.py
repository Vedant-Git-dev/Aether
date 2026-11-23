"""The one versioned policy file shared by the server and native builds.

Timing windows, rate limits and the AES-GCM budget live here as data, not as
literals scattered through endpoints. The loader is strict: unknown keys,
non-integer values and relations that would weaken the protocol are rejected at
startup rather than discovered at run time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DEFAULT_POLICY_PATH = Path(__file__).with_name("policy.v1.json")

_EXPECTED_KEYS = frozenset(
    {
        "v",
        "accept_window_seconds",
        "clock_skew_seconds",
        "grant_ceiling_seconds",
        "grant_ttl_min_seconds",
        "grant_ttl_max_seconds",
        "challenge_seconds",
        "device_session_seconds",
        "pair_ticket_seconds",
        "panel_intent_seconds",
        "ws_ticket_seconds",
        "rate_per_minute",
        "rate_burst",
        "renewal_numerator",
        "renewal_denominator",
        "nonce_ledger_seconds",
        "retry_base_millis",
        "retry_max_millis",
        "retry_jitter",
        "aes_gcm_budget",
        "aes_gcm_operational_threshold",
    }
)


@dataclass(frozen=True)
class VaultPolicy:
    v: int
    accept_window_seconds: int
    clock_skew_seconds: int
    grant_ceiling_seconds: int
    grant_ttl_min_seconds: int
    grant_ttl_max_seconds: int
    challenge_seconds: int
    device_session_seconds: int
    pair_ticket_seconds: int
    panel_intent_seconds: int
    ws_ticket_seconds: int
    rate_per_minute: int
    rate_burst: int
    renewal_numerator: int
    renewal_denominator: int
    nonce_ledger_seconds: int
    retry_base_millis: int
    retry_max_millis: int
    retry_jitter: bool
    aes_gcm_budget: int
    aes_gcm_operational_threshold: int


def _integer(data: dict[str, object], key: str) -> int:
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"policy {key} must be an integer")
    return value


def _load(path: Path) -> VaultPolicy:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("policy must be a JSON object")
    keys = set(data)
    if keys != _EXPECTED_KEYS:
        missing = sorted(_EXPECTED_KEYS - keys)
        extra = sorted(keys - _EXPECTED_KEYS)
        raise ValueError(f"policy keys differ: missing={missing} extra={extra}")

    jitter = data["retry_jitter"]
    if not isinstance(jitter, bool):
        raise ValueError("policy retry_jitter must be a boolean")

    policy = VaultPolicy(
        v=_integer(data, "v"),
        accept_window_seconds=_integer(data, "accept_window_seconds"),
        clock_skew_seconds=_integer(data, "clock_skew_seconds"),
        grant_ceiling_seconds=_integer(data, "grant_ceiling_seconds"),
        grant_ttl_min_seconds=_integer(data, "grant_ttl_min_seconds"),
        grant_ttl_max_seconds=_integer(data, "grant_ttl_max_seconds"),
        challenge_seconds=_integer(data, "challenge_seconds"),
        device_session_seconds=_integer(data, "device_session_seconds"),
        pair_ticket_seconds=_integer(data, "pair_ticket_seconds"),
        panel_intent_seconds=_integer(data, "panel_intent_seconds"),
        ws_ticket_seconds=_integer(data, "ws_ticket_seconds"),
        rate_per_minute=_integer(data, "rate_per_minute"),
        rate_burst=_integer(data, "rate_burst"),
        renewal_numerator=_integer(data, "renewal_numerator"),
        renewal_denominator=_integer(data, "renewal_denominator"),
        nonce_ledger_seconds=_integer(data, "nonce_ledger_seconds"),
        retry_base_millis=_integer(data, "retry_base_millis"),
        retry_max_millis=_integer(data, "retry_max_millis"),
        retry_jitter=jitter,
        aes_gcm_budget=_integer(data, "aes_gcm_budget"),
        aes_gcm_operational_threshold=_integer(data, "aes_gcm_operational_threshold"),
    )
    _check(policy)
    return policy


def _check(policy: VaultPolicy) -> None:
    if policy.v != 1:
        raise ValueError("policy version must be 1")
    if policy.accept_window_seconds <= 0 or policy.clock_skew_seconds < 0:
        raise ValueError("policy accept window and skew must be nonnegative")
    if not 0 < policy.grant_ttl_min_seconds <= policy.grant_ttl_max_seconds:
        raise ValueError("policy grant ttl range is invalid")
    if policy.grant_ttl_max_seconds > policy.grant_ceiling_seconds:
        raise ValueError("policy ttl maximum exceeds the grant ceiling")
    if policy.grant_ceiling_seconds > 300:
        raise ValueError("policy grant ceiling must not exceed 300 seconds")
    if policy.renewal_denominator <= 0:
        raise ValueError("policy renewal denominator must be positive")
    if not 0 < policy.renewal_numerator < policy.renewal_denominator:
        raise ValueError("policy renewal fraction must lie strictly inside 0..1")
    if policy.nonce_ledger_seconds < policy.accept_window_seconds + policy.clock_skew_seconds:
        raise ValueError("policy nonce ledger is shorter than the accept window plus skew")
    if policy.rate_per_minute <= 0 or policy.rate_burst <= 0:
        raise ValueError("policy rate limits must be positive")
    if not 0 < policy.retry_base_millis <= policy.retry_max_millis:
        raise ValueError("policy retry backoff range is invalid")
    if policy.aes_gcm_budget != 1 << 32:
        raise ValueError("policy AES-GCM budget must be 2^32 operations")
    if not 0 < policy.aes_gcm_operational_threshold < policy.aes_gcm_budget:
        raise ValueError("policy operational threshold must lie below the AES-GCM budget")


@lru_cache(maxsize=1)
def policy(path: str | Path | None = None) -> VaultPolicy:
    return _load(Path(path) if path is not None else DEFAULT_POLICY_PATH)
