"""Version 1 wire schema and strict validators.

This module is the Python definition of the bytes both endpoints exchange:
identifiers, categories, operations, signed body types, paths, bounds, error
codes and the parsers every endpoint and test double uses. The Kotlin client
mirrors it, and ``tests/vault/vectors`` pins the shared encodings.

Validation is deliberately total and strict. Unknown fields, repeated JSON
keys, floats, padded base64, unsorted or duplicated key refs and out-of-range
counters are rejected rather than coerced.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterable
from ipaddress import IPv6Address
from typing import Any
from urllib.parse import urlsplit

import rfc8785

from .errors import DENIAL_REASONS, ERROR_CODES, VAULT_STATES, VaultProtocolError
from .policy import policy

PROTOCOL_VERSION = 1
MEDIA_TYPE = "application/json"
API_PREFIX = "/api/v1/vault"

# ASCII order matters: key refs sort by category name, then version.
CATEGORIES = ("credentials", "ingress", "media", "memory", "operations", "workspace")

OPERATIONS = (
    "approved_dispatch",
    "credential_write",
    "drain",
    "index",
    "maintenance",
    "panel_read",
    "panel_write",
    "scheduled",
    "turn",
)

# The maximum category set an operation may ask for. The use-case registry may
# ask for a strict subset of this.
OPERATION_CATEGORIES: dict[str, frozenset[str]] = {
    "turn": frozenset({"memory", "workspace", "operations"}),
    "panel_read": frozenset({"memory", "workspace", "operations", "media"}),
    "panel_write": frozenset({"memory", "workspace", "operations", "media"}),
    "approved_dispatch": frozenset({"operations", "credentials", "media", "workspace", "memory"}),
    "drain": frozenset({"ingress", "operations", "memory", "media"}),
    "index": frozenset({"memory"}),
    "scheduled": frozenset({"operations", "memory", "workspace"}),
    "maintenance": frozenset(CATEGORIES),
    "credential_write": frozenset({"credentials"}),
}

# Serving callers must supply the approved, shared registry. Entries are
# added when the corresponding serving entry points are approved.
USE_CASES: dict[str, tuple[str, frozenset[str]]] = {}

MESSAGE_TYPES = (
    "control",
    "device_session",
    "lease_denied",
    "lease_grant",
    "lease_request",
    "manifest",
    "pair",
    "panel_consent",
    "push_registration",
    "restore",
)

CONTROL_OPS = ("enable", "lock", "retire")
ENTRY_STATUSES = ("active", "read_only")
WRAP_ALGS = ("android-keystore-aes-gcm", "ios-secure-enclave")
PUSH_PROVIDERS = ("apns", "fcm")
HPKE_USAGES = ("grant", "ingress", "recovery")

ID_BYTES = 16
NONCE_BYTES = 32
DIGEST_BYTES = 32
KEY_BYTES = 32
P256_PUBLIC_BYTES = 65
X25519_BYTES = 32
ED25519_BYTES = 32
IV_BYTES = 12
GCM_TAG_BYTES = 16
SIGNATURE_BYTES = 64
AE2_MAGIC = b"AE2\x00"
MAX_SAFE_INTEGER = (1 << 53) - 1

PATHS: dict[str, str] = {
    "status": "/status",
    "pair": "/pair",
    "challenge": "/challenge",
    "device_session": "/device-session",
    "manifest": "/manifest",
    "pending": "/pending",
    "grant": "/grant",
    "deny": "/deny",
    "control": "/control",
    "push_registration": "/push-registration",
    "panel_intent": "/panel-intent",
    "panel_consent": "/panel-consent",
    "panel_token": "/panel-token",
    "restore": "/restore",
    "ws_ticket": "/ws-ticket",
}

_B64U_RE = re.compile(r"^[A-Za-z0-9_-]*$")
_USE_CASE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_COMMON = frozenset({"v", "type", "vault", "device", "epoch"})


def vault_path(name: str) -> str:
    try:
        return API_PREFIX + PATHS[name]
    except KeyError as exc:
        raise VaultProtocolError("bad_field", f"unknown vault path {name!r}") from exc


# ---------------------------------------------------------------------------
# encodings


def b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64u_decode(value: Any, *, field: str, length: int | None = None) -> bytes:
    if not isinstance(value, str) or not value or "=" in value or not _B64U_RE.fullmatch(value):
        raise VaultProtocolError("bad_field", f"{field} is not unpadded base64url")
    if len(value) % 4 == 1:
        raise VaultProtocolError("bad_field", f"{field} has an impossible base64url length")
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if b64u_encode(raw) != value:
        raise VaultProtocolError("bad_field", f"{field} is not canonical base64url")
    if length is not None and len(raw) != length:
        raise VaultProtocolError("bad_field", f"{field} must decode to {length} bytes")
    return raw


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise VaultProtocolError("duplicate_field", f"repeated JSON key {key!r}")
        out[key] = value
    return out


def _reject_number(text: str) -> Any:
    raise VaultProtocolError("bad_field", "non-integer JSON number")


def loads_strict(data: str | bytes) -> Any:
    """Parse JSON with duplicate keys, floats and non-finite values rejected."""
    try:
        return json.loads(
            data,
            object_pairs_hook=_no_duplicates,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except VaultProtocolError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise VaultProtocolError("malformed", "body is not valid JSON") from exc


def canonical_bytes(value: Any) -> bytes:
    try:
        return rfc8785.dumps(value)
    except (TypeError, ValueError) as exc:
        raise VaultProtocolError("bad_field", "value is not canonical JSON") from exc


# ---------------------------------------------------------------------------
# scalar and container helpers


def _object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VaultProtocolError("bad_field", f"{where} must be an object")
    return value


def _exact(obj: Any, keys: Iterable[str], where: str) -> dict[str, Any]:
    data = _object(obj, where)
    expected = frozenset(keys)
    actual = frozenset(data)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise VaultProtocolError(
            "bad_field", f"{where} fields differ: missing={missing} extra={extra}"
        )
    return data


def _int(value: Any, where: str, *, minimum: int = 0, maximum: int = MAX_SAFE_INTEGER) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise VaultProtocolError("bad_field", f"{where} must be an integer")
    if not minimum <= value <= maximum:
        raise VaultProtocolError("bad_field", f"{where} is out of range")
    return value


def _text(value: Any, where: str, *, maximum: int = 2048) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise VaultProtocolError("bad_field", f"{where} must be a non-empty string")
    return value


def _bytes(value: Any, where: str, length: int) -> bytes:
    return b64u_decode(value, field=where, length=length)


def _digest(value: Any, where: str) -> bytes:
    return _bytes(value, where, DIGEST_BYTES)


def _version(value: Any, where: str) -> int:
    return _int(value, where, minimum=1)


def _epoch(value: Any, where: str) -> int:
    return _int(value, where, minimum=1)


def normalize_origin(value: Any) -> str:
    """Return the pinned origin form: https, ASCII host, no path/query/userinfo."""
    if not isinstance(value, str) or not value or not value.isascii():
        raise VaultProtocolError("bad_field", "origin must be an ASCII string")
    if any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise VaultProtocolError("bad_field", "origin contains whitespace or controls")
    if any(char in value for char in "?#\\%@"):
        raise VaultProtocolError("bad_field", "origin contains a forbidden delimiter")
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
        if parts.scheme != "https" or parts.path or not host:
            raise ValueError
        if ":" in host:
            authority = r"\[[0-9a-fA-F:.]+\](?::[0-9]+)?"
            host = IPv6Address(host).compressed
        else:
            label = r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
            authority = rf"{label}(?:\.{label})*(?::[0-9]+)?"
        if not re.fullmatch(authority, parts.netloc) or len(host) > 253 or port == 0:
            raise ValueError
    except ValueError as exc:
        raise VaultProtocolError("bad_field", "invalid HTTPS origin") from exc

    shown = f"[{host}]" if ":" in host else host
    if port is not None and port != 443:
        shown = f"{shown}:{port}"
    return f"https://{shown}"


# ---------------------------------------------------------------------------
# refs, use cases and common fields


def validate_refs(value: Any) -> list[tuple[str, int]]:
    if not isinstance(value, list) or not value:
        raise VaultProtocolError("bad_field", "refs must be a non-empty array")
    seen: set[tuple[str, int]] = set()
    refs: list[tuple[str, int]] = []
    for index, raw in enumerate(value):
        ref = _exact(raw, ("cat", "ver"), f"refs[{index}]")
        cat = ref["cat"]
        if cat not in CATEGORIES:
            raise VaultProtocolError("bad_field", f"refs[{index}].cat is not a category")
        ver = _version(ref["ver"], f"refs[{index}].ver")
        key = (cat, ver)
        if key in seen:
            raise VaultProtocolError("bad_field", "refs must be unique")
        seen.add(key)
        refs.append(key)
    if refs != sorted(refs):
        raise VaultProtocolError("bad_field", "refs must be sorted by category then version")
    return refs


def validate_use_case(use_case: Any, operation: Any) -> frozenset[str]:
    if operation not in OPERATIONS:
        raise VaultProtocolError("bad_field", "operation is not in the closed enum")
    if not isinstance(use_case, str) or not _USE_CASE_RE.fullmatch(use_case):
        raise VaultProtocolError("bad_field", "use_case is not a registry ID")
    entry = USE_CASES.get(use_case)
    if entry is None:
        raise VaultProtocolError("bad_field", "use_case is not registered")
    declared_operation, categories = entry
    if declared_operation != operation:
        raise VaultProtocolError("bad_field", "use_case does not match the operation")
    return categories & OPERATION_CATEGORIES[operation]


def _validate_common(body: Any, expected_type: str | None = None) -> dict[str, Any]:
    data = _object(body, "body")
    if not _COMMON.issubset(data):
        raise VaultProtocolError("bad_field", "body is missing a common field")
    version = data["v"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise VaultProtocolError("bad_field", "v must be an integer")
    if version != PROTOCOL_VERSION:
        raise VaultProtocolError("bad_version", "unsupported protocol version")
    kind = data["type"]
    if kind not in MESSAGE_TYPES:
        raise VaultProtocolError("bad_field", "type is not in the closed enum")
    if expected_type is not None and kind != expected_type:
        raise VaultProtocolError("bad_field", f"expected type {expected_type}")
    _bytes(data["vault"], "vault", ID_BYTES)
    _bytes(data["device"], "device", ID_BYTES)
    _epoch(data["epoch"], "epoch")
    return data


def _exact_body(body: Any, extra: Iterable[str], kind: str) -> dict[str, Any]:
    data = _validate_common(body, kind)
    return _exact(data, _COMMON | frozenset(extra), "body")


# ---------------------------------------------------------------------------
# signed envelopes


def validate_envelope(envelope: Any) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    """Validate the detached-JWS shape and return (body, header, signature)."""
    obj = _exact(envelope, ("body", "protected", "signature"), "signed object")
    protected = _text(obj["protected"], "protected", maximum=4096)
    header = loads_strict(b64u_decode(protected, field="protected"))
    header = _exact(header, ("alg", "kid"), "protected header")
    if header["alg"] not in ("ES256", "Ed25519"):
        raise VaultProtocolError("bad_field", "protected alg is not allowed")
    _bytes(header["kid"], "protected kid", DIGEST_BYTES)
    if b64u_encode(canonical_bytes(header)) != protected:
        raise VaultProtocolError("bad_field", "protected header is not canonical")
    signature = _bytes(obj["signature"], "signature", SIGNATURE_BYTES)
    body = _object(obj["body"], "body")
    return body, header, signature


# ---------------------------------------------------------------------------
# body validators


def validate_pair(body: Any) -> dict[str, Any]:
    data = _exact_body(
        body,
        ("origin", "server_kid", "phone_pub", "manifest_digest", "nonce", "iat"),
        "pair",
    )
    if normalize_origin(data["origin"]) != data["origin"]:
        raise VaultProtocolError("bad_field", "origin must use its canonical form")
    _bytes(data["server_kid"], "server_kid", DIGEST_BYTES)
    _bytes(data["phone_pub"], "phone_pub", P256_PUBLIC_BYTES)
    _digest(data["manifest_digest"], "manifest_digest")
    _bytes(data["nonce"], "nonce", NONCE_BYTES)
    _int(data["iat"], "iat")
    return data


def validate_device_session(body: Any) -> dict[str, Any]:
    data = _exact_body(body, ("challenge", "iat"), "device_session")
    _bytes(data["challenge"], "challenge", NONCE_BYTES)
    _int(data["iat"], "iat")
    return data


def validate_panel_consent(body: Any) -> dict[str, Any]:
    data = _exact_body(body, ("intent", "challenge_hash", "iat"), "panel_consent")
    _bytes(data["intent"], "intent", ID_BYTES)
    _digest(data["challenge_hash"], "challenge_hash")
    _int(data["iat"], "iat")
    return data


def validate_control(body: Any) -> dict[str, Any]:
    data = _exact_body(body, ("op", "next_epoch", "challenge", "iat"), "control")
    if data["op"] not in CONTROL_OPS:
        raise VaultProtocolError("bad_field", "control op is not in the closed enum")
    next_epoch = _epoch(data["next_epoch"], "next_epoch")
    if next_epoch != data["epoch"] + 1:
        raise VaultProtocolError("bad_field", "control must advance the epoch by one")
    _bytes(data["challenge"], "challenge", NONCE_BYTES)
    _int(data["iat"], "iat")
    return data


def validate_manifest(body: Any) -> dict[str, Any]:
    data = _exact_body(
        body,
        (
            "revision",
            "previous_digest",
            "ingress_public",
            "recovery_public",
            "recovery_verify",
            "entries",
        ),
        "manifest",
    )
    revision = _version(data["revision"], "revision")
    previous = data["previous_digest"]
    if revision == 1:
        if previous is not None:
            raise VaultProtocolError("bad_field", "a genesis manifest has no previous digest")
    else:
        _digest(previous, "previous_digest")

    ingress = data["ingress_public"]
    if not isinstance(ingress, list) or not ingress:
        raise VaultProtocolError("bad_field", "ingress_public must be a non-empty array")
    ingress_versions: list[int] = []
    for index, raw in enumerate(ingress):
        item = _exact(raw, ("ver", "public"), f"ingress_public[{index}]")
        ver = _version(item["ver"], f"ingress_public[{index}].ver")
        _bytes(item["public"], f"ingress_public[{index}].public", X25519_BYTES)
        ingress_versions.append(ver)
    if ingress_versions != sorted(set(ingress_versions)):
        raise VaultProtocolError("bad_field", "ingress_public must be unique and sorted by version")

    _bytes(data["recovery_public"], "recovery_public", X25519_BYTES)
    _bytes(data["recovery_verify"], "recovery_verify", ED25519_BYTES)

    entries = data["entries"]
    if not isinstance(entries, list) or not entries:
        raise VaultProtocolError("bad_field", "entries must be a non-empty array")
    seen: set[tuple[str, int]] = set()
    ordered: list[tuple[str, int]] = []
    active: dict[str, int] = {}
    ingress_entry_versions: list[int] = []
    for index, raw in enumerate(entries):
        entry = _exact(raw, ("cat", "ver", "status", "wrap", "backup"), f"entries[{index}]")
        cat = entry["cat"]
        if cat not in CATEGORIES:
            raise VaultProtocolError("bad_field", f"entries[{index}].cat is not a category")
        ver = _version(entry["ver"], f"entries[{index}].ver")
        key = (cat, ver)
        if key in seen:
            raise VaultProtocolError("bad_field", "entries must be unique")
        seen.add(key)
        ordered.append(key)
        if entry["status"] not in ENTRY_STATUSES:
            raise VaultProtocolError("bad_field", f"entries[{index}].status is not allowed")
        if entry["status"] == "active":
            if cat in active:
                raise VaultProtocolError("bad_field", f"category {cat} has two active entries")
            active[cat] = ver
        wrap = _exact(entry["wrap"], ("alg", "blob"), f"entries[{index}].wrap")
        if wrap["alg"] not in WRAP_ALGS:
            raise VaultProtocolError("bad_field", f"entries[{index}].wrap.alg is not allowed")
        _text(wrap["blob"], f"entries[{index}].wrap.blob", maximum=4096)
        b64u_decode(wrap["blob"], field=f"entries[{index}].wrap.blob")
        backup = _exact(entry["backup"], ("enc", "ct"), f"entries[{index}].backup")
        _bytes(backup["enc"], f"entries[{index}].backup.enc", X25519_BYTES)
        _text(backup["ct"], f"entries[{index}].backup.ct", maximum=4096)
        b64u_decode(backup["ct"], field=f"entries[{index}].backup.ct")
        if cat == "ingress":
            ingress_entry_versions.append(ver)
    if ordered != sorted(ordered):
        raise VaultProtocolError("bad_field", "entries must be sorted by category then version")
    if set(active) != set(CATEGORIES):
        raise VaultProtocolError("bad_field", "every category needs exactly one active entry")
    if sorted(ingress_entry_versions) != ingress_versions:
        raise VaultProtocolError("bad_field", "ingress_public does not match the ingress entries")
    return data


def validate_push_registration(body: Any) -> dict[str, Any]:
    data = _exact_body(body, ("challenge", "provider", "registration", "iat"), "push_registration")
    _bytes(data["challenge"], "challenge", NONCE_BYTES)
    if data["provider"] not in PUSH_PROVIDERS:
        raise VaultProtocolError("bad_field", "provider is not in the closed enum")
    _bytes(data["registration"], "registration", NONCE_BYTES)
    _int(data["iat"], "iat")
    return data


def validate_lease_request(body: Any) -> dict[str, Any]:
    data = _exact_body(
        body,
        (
            "boot",
            "task",
            "operation",
            "use_case",
            "request",
            "nonce",
            "iat",
            "accept_until",
            "refs",
            "ttl",
            "epk",
            "manifest_digest",
        ),
        "lease_request",
    )
    _bytes(data["boot"], "boot", ID_BYTES)
    _bytes(data["task"], "task", ID_BYTES)
    _bytes(data["request"], "request", ID_BYTES)
    _bytes(data["nonce"], "nonce", NONCE_BYTES)
    _bytes(data["epk"], "epk", X25519_BYTES)
    _digest(data["manifest_digest"], "manifest_digest")
    iat = _int(data["iat"], "iat")
    accept_until = _int(data["accept_until"], "accept_until")
    expected = iat + policy().accept_window_seconds
    if accept_until != expected:
        raise VaultProtocolError("bad_field", "accept_until must equal iat plus the accept window")
    ttl = _int(
        data["ttl"],
        "ttl",
        minimum=policy().grant_ttl_min_seconds,
        maximum=policy().grant_ttl_max_seconds,
    )
    refs = validate_refs(data["refs"])
    allowed = validate_use_case(data["use_case"], data["operation"])
    if not {cat for cat, _ in refs} <= allowed:
        raise VaultProtocolError("bad_scope", "refs exceed the use-case category set")
    if ttl > policy().grant_ceiling_seconds:
        raise VaultProtocolError("bad_field", "ttl exceeds the grant ceiling")
    return data


def validate_lease_grant(body: Any) -> dict[str, Any]:
    data = _exact_body(
        body,
        (
            "boot",
            "task",
            "request",
            "nonce",
            "request_digest",
            "lease",
            "iat",
            "exp",
            "refs",
            "manifest_digest",
            "box",
        ),
        "lease_grant",
    )
    _bytes(data["boot"], "boot", ID_BYTES)
    _bytes(data["task"], "task", ID_BYTES)
    _bytes(data["request"], "request", ID_BYTES)
    _bytes(data["lease"], "lease", ID_BYTES)
    _bytes(data["nonce"], "nonce", NONCE_BYTES)
    _digest(data["request_digest"], "request_digest")
    _digest(data["manifest_digest"], "manifest_digest")
    iat = _int(data["iat"], "iat")
    exp = _int(data["exp"], "exp")
    if exp <= iat:
        raise VaultProtocolError("bad_field", "exp must be after iat")
    if exp - iat > policy().grant_ceiling_seconds:
        raise VaultProtocolError("bad_field", "grant lifetime exceeds the ceiling")
    validate_refs(data["refs"])
    box = _exact(data["box"], ("enc", "ct"), "box")
    _bytes(box["enc"], "box.enc", X25519_BYTES)
    _text(box["ct"], "box.ct", maximum=65536)
    if len(b64u_decode(box["ct"], field="box.ct")) < GCM_TAG_BYTES:
        raise VaultProtocolError("bad_field", "box.ct is too short")
    return data


def validate_lease_denied(body: Any) -> dict[str, Any]:
    data = _exact_body(
        body,
        ("boot", "task", "request", "nonce", "request_digest", "iat", "reason"),
        "lease_denied",
    )
    _bytes(data["boot"], "boot", ID_BYTES)
    _bytes(data["task"], "task", ID_BYTES)
    _bytes(data["request"], "request", ID_BYTES)
    _bytes(data["nonce"], "nonce", NONCE_BYTES)
    _digest(data["request_digest"], "request_digest")
    _int(data["iat"], "iat")
    if data["reason"] not in DENIAL_REASONS:
        raise VaultProtocolError("bad_field", "reason is not in the closed enum")
    return data


def validate_restore(body: Any) -> dict[str, Any]:
    data = _exact_body(
        body,
        ("challenge", "previous_digest", "new_manifest_digest", "new_phone_pub", "iat"),
        "restore",
    )
    _bytes(data["challenge"], "challenge", NONCE_BYTES)
    _digest(data["previous_digest"], "previous_digest")
    _digest(data["new_manifest_digest"], "new_manifest_digest")
    _bytes(data["new_phone_pub"], "new_phone_pub", P256_PUBLIC_BYTES)
    _int(data["iat"], "iat")
    return data


_VALIDATORS = {
    "control": validate_control,
    "device_session": validate_device_session,
    "lease_denied": validate_lease_denied,
    "lease_grant": validate_lease_grant,
    "lease_request": validate_lease_request,
    "manifest": validate_manifest,
    "pair": validate_pair,
    "panel_consent": validate_panel_consent,
    "push_registration": validate_push_registration,
    "restore": validate_restore,
}


def validate_body(body: Any, *, expected_type: str | None = None) -> dict[str, Any]:
    data = _validate_common(body, expected_type)
    validator = _VALIDATORS[data["type"]]
    return validator(data)


# ---------------------------------------------------------------------------
# small response bodies


def validate_error_payload(payload: Any) -> dict[str, str]:
    data = _exact(payload, ("code",), "error payload")
    code = data["code"]
    if code not in ERROR_CODES:
        raise VaultProtocolError("bad_field", "error code is not in the closed catalog")
    return {"code": code}


def validate_status_payload(payload: Any) -> dict[str, Any]:
    data = _exact(payload, ("state", "epoch"), "status payload")
    if data["state"] not in VAULT_STATES:
        raise VaultProtocolError("bad_field", "status state is not locked or available")
    _epoch(data["epoch"], "epoch")
    return data
