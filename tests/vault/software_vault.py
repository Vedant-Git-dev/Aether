"""A tests-only software stand-in for the native vault.

This is not shipped and is never imported by ``src/aether``. It implements the
phone side of the wire contract so the protocol, vectors and later lease and
restore tests can run without hardware: enroll, issue, deny, lock and restore.
Keys are ordinary process memory here; the real implementation keeps them in
hardware-backed, non-exportable storage.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

import reference_wire as wire
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from aether.vault import schema
from aether.vault.errors import VaultProtocolError
from aether.vault.policy import policy


class VaultDenied(Exception):
    """A valid request that policy refused, carrying the signed denial."""

    def __init__(self, reason: str, envelope: dict[str, Any]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.envelope = envelope


@dataclass(frozen=True)
class Issued:
    body: dict[str, Any]
    envelope: dict[str, Any]


def _p256_keypair(seed: bytes | None = None) -> tuple[bytes, bytes]:
    if seed is None:
        private = ec.generate_private_key(ec.SECP256R1())
    else:
        if len(seed) != 32:
            raise ValueError("P-256 seed must be 32 bytes")
        private = ec.derive_private_key(int.from_bytes(seed, "big"), ec.SECP256R1())
    raw = private.private_numbers().private_value.to_bytes(32, "big")
    public = private.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return raw, public


def _x25519_keypair(seed: bytes | None = None) -> tuple[bytes, bytes]:
    private = (
        X25519PrivateKey.generate() if seed is None else X25519PrivateKey.from_private_bytes(seed)
    )
    return private.private_bytes_raw(), private.public_key().public_bytes_raw()


class SoftwareVault:
    def __init__(
        self,
        *,
        vault_id: bytes,
        device_id: bytes,
        origin: str,
        server_public: bytes,
        server_kid: str,
        phone_private: bytes | None = None,
        hardware_master: bytes | None = None,
        category_keys: dict[tuple[str, int], bytes] | None = None,
        ingress_private: bytes | None = None,
        recovery_entropy: bytes | None = None,
    ) -> None:
        if len(vault_id) != schema.ID_BYTES or len(device_id) != schema.ID_BYTES:
            raise ValueError("vault and device ids must be 16 bytes")
        self.vault_id = vault_id
        self.device_id = device_id
        self.origin = schema.normalize_origin(origin)
        self.server_public = server_public
        self.server_kid = server_kid
        self.hardware_master = hardware_master or secrets.token_bytes(schema.KEY_BYTES)
        self.phone_private, self.phone_public = _p256_keypair(phone_private)
        self.phone_kid = wire.signer_id(self.phone_public)
        self.category_keys = dict(category_keys or {})
        self.ingress_private = ingress_private
        self.recovery_entropy = recovery_entropy or secrets.token_bytes(32)
        self.recovery = wire.recovery_keys(self.recovery_entropy, vault_id)
        self.phrase = wire.phrase_from_entropy(self.recovery_entropy)
        self.epoch = 1
        self.state = "locked"
        self.manifest_body: dict[str, Any] = {}
        self.manifest: dict[str, Any] = {}
        self._consumed: dict[tuple[str, str], int] = {}
        self._consents: set[bytes] = set()
        self._tokens = float(policy().rate_burst)
        self._token_time = 0
        self._leases: dict[bytes, dict[str, Any]] = {}

    # -- lifecycle ---------------------------------------------------------

    def enroll(self) -> dict[str, Any]:
        if not self.category_keys:
            for cat in schema.CATEGORIES:
                if cat == "ingress":
                    self.ingress_private, _ = _x25519_keypair(self.ingress_private)
                    self.category_keys[cat, 1] = self.ingress_private
                else:
                    self.category_keys[cat, 1] = secrets.token_bytes(schema.KEY_BYTES)
        if self.ingress_private is None:
            raise ValueError("ingress private key is missing")
        ingress_public = (
            X25519PrivateKey.from_private_bytes(self.ingress_private)
            .public_key()
            .public_bytes_raw()
        )
        entries = [
            self._entry(cat, 1, "active", self.category_keys[cat, 1]) for cat in schema.CATEGORIES
        ]
        body = {
            "v": schema.PROTOCOL_VERSION,
            "type": "manifest",
            "vault": schema.b64u_encode(self.vault_id),
            "device": schema.b64u_encode(self.device_id),
            "epoch": self.epoch,
            "revision": 1,
            "previous_digest": None,
            "ingress_public": [
                {"ver": 1, "public": schema.b64u_encode(ingress_public)},
            ],
            "recovery_public": schema.b64u_encode(self.recovery.hpke_public),
            "recovery_verify": schema.b64u_encode(self.recovery.sign_public),
            "entries": entries,
        }
        schema.validate_body(body)
        self.manifest_body = body
        self.manifest = wire.sign_body(
            body, wire.ec_private_key(self.phone_private), alg="ES256", kid=self.phone_kid
        )
        return self.manifest

    def enable(self) -> None:
        self.epoch += 1
        self.state = "available"

    def lock(self) -> None:
        self.epoch += 1
        self.state = "locked"
        self._leases.clear()
        self._consents.clear()

    # -- lease exchange ----------------------------------------------------

    def consent(self, request_envelope: Any) -> None:
        """Simulate local owner consent bound to one exact signed request."""
        body = self._verified_request(request_envelope)
        if self.state != "available" or body["epoch"] != self.epoch:
            raise VaultProtocolError("denied", "request is outside the current enabled epoch")
        self._consents.add(wire.body_digest(body))

    def issue(self, request_envelope: Any, *, now: int) -> Issued:
        body = self._verified_request(request_envelope)
        reason = self._admission(body, now)
        if reason is not None:
            raise VaultDenied(reason, self.deny(body, reason, now=now))

        ttl = body["ttl"]
        exp = min(now + ttl, body["iat"] + ttl)
        refs = schema.validate_refs(body["refs"])
        lease = secrets.token_bytes(schema.ID_BYTES)
        header: dict[str, Any] = {
            "v": schema.PROTOCOL_VERSION,
            "type": "lease_grant",
            "vault": schema.b64u_encode(self.vault_id),
            "device": schema.b64u_encode(self.device_id),
            "epoch": self.epoch,
            "boot": body["boot"],
            "task": body["task"],
            "request": body["request"],
            "nonce": body["nonce"],
            "request_digest": schema.b64u_encode(wire.body_digest(body)),
            "lease": schema.b64u_encode(lease),
            "iat": now,
            "exp": exp,
            "refs": [{"cat": cat, "ver": ver} for cat, ver in refs],
            "manifest_digest": schema.b64u_encode(wire.body_digest(self.manifest_body)),
        }
        plaintext = schema.canonical_bytes(
            {
                "keys": [
                    {
                        "cat": cat,
                        "ver": ver,
                        "key": schema.b64u_encode(self.category_keys[cat, ver]),
                    }
                    for cat, ver in refs
                ]
            }
        )
        header["box"] = wire.hpke_seal(
            schema.b64u_decode(body["epk"], field="epk", length=schema.X25519_BYTES),
            "grant",
            header,
            plaintext,
        )
        schema.validate_body(header)
        envelope = wire.sign_body(
            header, wire.ec_private_key(self.phone_private), alg="ES256", kid=self.phone_kid
        )
        self._consents.discard(wire.body_digest(body))
        self._consumed[body["request"], body["nonce"]] = (
            body["accept_until"] + policy().clock_skew_seconds
        )
        self._leases[schema.b64u_decode(body["task"], field="task")] = {
            "lease": lease,
            "refs": refs,
            "exp": exp,
        }
        return Issued(body=header, envelope=envelope)

    def deny(self, request_body: Any, reason: str, *, now: int) -> dict[str, Any]:
        schema.validate_body(request_body, expected_type="lease_request")
        if reason not in schema.DENIAL_REASONS:
            raise VaultProtocolError("bad_field", "denial reason is not in the closed enum")
        denial = {
            "v": schema.PROTOCOL_VERSION,
            "type": "lease_denied",
            "vault": schema.b64u_encode(self.vault_id),
            "device": schema.b64u_encode(self.device_id),
            "epoch": self.epoch,
            "boot": request_body["boot"],
            "task": request_body["task"],
            "request": request_body["request"],
            "nonce": request_body["nonce"],
            "request_digest": schema.b64u_encode(wire.body_digest(request_body)),
            "iat": now,
            "reason": reason,
        }
        schema.validate_body(denial)
        return wire.sign_body(
            denial, wire.ec_private_key(self.phone_private), alg="ES256", kid=self.phone_kid
        )

    # -- restore -----------------------------------------------------------

    def restore(
        self,
        phrase: str,
        backup_manifest: dict[str, Any],
        *,
        new_device_id: bytes,
        new_phone_private: bytes | None = None,
        new_hardware_master: bytes | None = None,
        now: int,
    ) -> dict[str, Any]:
        entropy = wire.entropy_from_phrase(phrase)
        recovery = wire.recovery_keys(entropy, self.vault_id)
        if recovery.sign_public != self.recovery.sign_public:
            raise VaultProtocolError("bad_signature", "phrase does not match this vault")
        body = wire.verify_body(
            backup_manifest,
            wire.ec_public_key(self.phone_public),
            expected_kid=self.phone_kid,
        )
        schema.validate_body(body, expected_type="manifest")
        pinned_revision = self.manifest_body["revision"]
        pinned_digest = wire.body_digest(self.manifest_body)
        pinned_backup = (
            body["revision"] == pinned_revision and wire.body_digest(body) == pinned_digest
        )
        verified_successor = body["revision"] == pinned_revision + 1 and body[
            "previous_digest"
        ] == schema.b64u_encode(pinned_digest)
        if (
            schema.b64u_decode(body["vault"], field="vault") != self.vault_id
            or schema.b64u_decode(body["device"], field="device") != self.device_id
            or body["recovery_public"] != schema.b64u_encode(recovery.hpke_public)
            or body["recovery_verify"] != schema.b64u_encode(recovery.sign_public)
            or not (pinned_backup or verified_successor)
        ):
            raise VaultProtocolError("denied", "backup does not match the pinned vault")
        if len(new_device_id) != schema.ID_BYTES or new_device_id == self.device_id:
            raise VaultProtocolError("bad_field", "restore requires a new device ID")

        keys = {}
        for entry in body["entries"]:
            cat, ver = entry["cat"], entry["ver"]
            keys[cat, ver] = wire.backup_unwrap(
                recovery.hpke_private, self.vault_id, cat, ver, entry["backup"]
            )
        for item in body["ingress_public"]:
            public = X25519PrivateKey.from_private_bytes(keys["ingress", item["ver"]]).public_key()
            if schema.b64u_encode(public.public_bytes_raw()) != item["public"]:
                raise VaultProtocolError("bad_field", "backup ingress key does not match")

        master = new_hardware_master or secrets.token_bytes(schema.KEY_BYTES)
        private, public = _p256_keypair(new_phone_private)
        kid = wire.signer_id(public)
        epoch = max(self.epoch, body["epoch"]) + 1
        entries = []
        for entry in body["entries"]:
            cat, ver = entry["cat"], entry["ver"]
            entries.append(
                {
                    **entry,
                    "wrap": wire.device_wrap(master, self.vault_id, cat, ver, keys[cat, ver]),
                }
            )
        new_body = {
            **body,
            "device": schema.b64u_encode(new_device_id),
            "epoch": epoch,
            "revision": body["revision"] + 1,
            "previous_digest": schema.b64u_encode(wire.body_digest(body)),
            "entries": entries,
        }
        schema.validate_body(new_body)
        manifest = wire.sign_body(new_body, wire.ec_private_key(private), alg="ES256", kid=kid)

        self.device_id = new_device_id
        self.hardware_master = master
        self.phone_private, self.phone_public, self.phone_kid = private, public, kid
        self.category_keys = keys
        self.ingress_private = next(
            keys["ingress", entry["ver"]]
            for entry in entries
            if entry["cat"] == "ingress" and entry["status"] == "active"
        )
        self.epoch = epoch
        self.state = "locked"
        self._leases.clear()
        self._consents.clear()
        self._consumed.clear()
        self.manifest_body, self.manifest = new_body, manifest
        return manifest

    # -- internals ---------------------------------------------------------

    def _entry(self, cat: str, ver: int, status: str, key: bytes) -> dict[str, Any]:
        return {
            "cat": cat,
            "ver": ver,
            "status": status,
            "wrap": wire.device_wrap(self.hardware_master, self.vault_id, cat, ver, key),
            "backup": wire.backup_wrap(self.recovery.hpke_public, self.vault_id, cat, ver, key),
        }

    def _verified_request(self, envelope: Any) -> dict[str, Any]:
        body = wire.verify_body(
            envelope, wire.ec_public_key(self.server_public), expected_kid=self.server_kid
        )
        schema.validate_body(body, expected_type="lease_request")
        if body["vault"] != schema.b64u_encode(self.vault_id) or body[
            "device"
        ] != schema.b64u_encode(self.device_id):
            raise VaultProtocolError("denied", "request does not match the paired device")
        return body

    def _admission(self, body: dict[str, Any], now: int) -> str | None:
        if self.state != "available":
            return "locked"
        if body["epoch"] != self.epoch:
            return "stale"
        if body["manifest_digest"] != schema.b64u_encode(wire.body_digest(self.manifest_body)):
            return "manifest"
        skew = policy().clock_skew_seconds
        if (
            body["iat"] > now + skew
            or now > body["accept_until"]
            or now >= body["iat"] + body["ttl"]
        ):
            return "stale"
        self._consumed = {
            pair: deadline for pair, deadline in self._consumed.items() if now <= deadline
        }
        if any(
            body["request"] == request or body["nonce"] == nonce
            for request, nonce in self._consumed
        ):
            return "stale"
        if not self._take_token(now):
            return "rate"
        refs = schema.validate_refs(body["refs"])
        for cat, ver in refs:
            entry = next(
                (
                    item
                    for item in self.manifest_body["entries"]
                    if item["cat"] == cat and item["ver"] == ver
                ),
                None,
            )
            if entry is None:
                return "manifest"
            if entry["status"] == "read_only" and body["operation"] not in (
                "drain",
                "panel_read",
                "maintenance",
            ):
                return "bad_scope"
        if body["operation"] in ("maintenance", "credential_write") and (
            wire.body_digest(body) not in self._consents
        ):
            return "user_required"
        return None

    def _take_token(self, now: int) -> bool:
        conf = policy()
        elapsed = max(0, now - self._token_time)
        self._tokens = min(
            float(conf.rate_burst),
            self._tokens + elapsed * conf.rate_per_minute / 60.0,
        )
        self._token_time = max(now, self._token_time)
        if self._tokens < 1.0:
            return False
        self._tokens -= 1.0
        return True


def open_grant(
    grant: dict[str, Any],
    request_body: dict[str, Any],
    *,
    phone_public: bytes,
    boot_private: bytes,
    now: int,
) -> tuple[dict[str, Any], dict[tuple[str, int], bytes]]:
    """Verify against a current outstanding request before opening its grant."""
    schema.validate_body(request_body, expected_type="lease_request")
    body = wire.verify_body(
        grant, wire.ec_public_key(phone_public), expected_kid=wire.signer_id(phone_public)
    )
    schema.validate_body(body, expected_type="lease_grant")
    bound = (
        "vault",
        "device",
        "epoch",
        "boot",
        "task",
        "request",
        "nonce",
        "manifest_digest",
        "refs",
    )
    expected_exp = min(body["iat"], request_body["iat"]) + request_body["ttl"]
    if (
        any(body[field] != request_body[field] for field in bound)
        or body["request_digest"] != schema.b64u_encode(wire.body_digest(request_body))
        or body["exp"] != expected_exp
        or now > request_body["accept_until"]
        or now >= body["exp"]
    ):
        raise VaultProtocolError("denied", "grant does not match a current request")
    header = {field: value for field, value in body.items() if field != "box"}
    plaintext = wire.hpke_open(boot_private, "grant", header, body["box"])
    payload = schema.loads_strict(plaintext)
    refs = schema.validate_refs(body["refs"])
    if (
        not isinstance(payload, dict)
        or payload.keys() != {"keys"}
        or not isinstance(payload["keys"], list)
        or len(payload["keys"]) != len(refs)
        or schema.canonical_bytes(payload) != plaintext
    ):
        raise VaultProtocolError("bad_field", "invalid grant payload")
    keys = {}
    for item, (cat, ver) in zip(payload["keys"], refs, strict=True):
        if (
            not isinstance(item, dict)
            or item.keys() != {"cat", "ver", "key"}
            or item["cat"] != cat
            or type(item["ver"]) is not int
            or item["ver"] != ver
        ):
            raise VaultProtocolError("bad_field", "grant payload key does not match its reference")
        keys[cat, ver] = schema.b64u_decode(item["key"], field="key", length=schema.KEY_BYTES)
    return body, keys
