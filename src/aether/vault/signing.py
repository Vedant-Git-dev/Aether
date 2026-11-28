"""Detached ES256 envelopes for pinned vault signers."""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from joserfc import jws
from joserfc.jwk import ECKey

from .errors import VaultProtocolError
from .schema import (
    KEY_BYTES,
    P256_PUBLIC_BYTES,
    b64u_decode,
    b64u_encode,
    canonical_bytes,
    validate_body,
    validate_envelope,
)

_ALGORITHM = "ES256"
_SIGNATURE = jws.construct_registry([_ALGORITHM]).get_alg(_ALGORITHM)


def _signer_id(public: bytes) -> str:
    return b64u_encode(sha256(public).digest())


def _signing_input(protected: str, body: dict[str, Any]) -> bytes:
    return f"{protected}.{b64u_encode(canonical_bytes(body))}".encode("ascii")


class RequestSigner:
    """Installation signer loaded from trusted provisioning by the serving boot."""

    def __init__(self, private: ec.EllipticCurvePrivateKey) -> None:
        if not isinstance(private.curve, ec.SECP256R1):
            raise ValueError("the request signer must be P-256")
        self._key = ECKey.import_key(private)
        self.public = private.public_key().public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint
        )
        self.kid = _signer_id(self.public)
        self._protected = b64u_encode(canonical_bytes({"alg": _ALGORITHM, "kid": self.kid}))

    def sign_request(self, body: dict[str, Any]) -> dict[str, Any]:
        validate_body(body, expected_type="lease_request")
        signature = _SIGNATURE.sign(_signing_input(self._protected, body), self._key)
        return {"body": body, "protected": self._protected, "signature": b64u_encode(signature)}

    @classmethod
    def from_scalar(cls, encoded: str) -> RequestSigner:
        scalar = int.from_bytes(
            b64u_decode(encoded, field="request signer", length=KEY_BYTES), "big"
        )
        return cls(ec.derive_private_key(scalar, ec.SECP256R1()))


def verify_es256(envelope: Any, *, pinned_public: bytes, expected_type: str) -> dict[str, Any]:
    """Verify the pinned signer before interpreting any signed body fields."""
    body, header, signature = validate_envelope(envelope)
    if header["alg"] != _ALGORITHM or header["kid"] != _signer_id(pinned_public):
        raise VaultProtocolError("bad_signature")
    if len(pinned_public) != P256_PUBLIC_BYTES or pinned_public[0] != 4:
        raise VaultProtocolError("bad_signature")
    try:
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), pinned_public)
    except ValueError:
        raise VaultProtocolError("bad_signature") from None
    if not _SIGNATURE.verify(
        _signing_input(envelope["protected"], body), signature, ECKey.import_key(key)
    ):
        raise VaultProtocolError("bad_signature")
    return validate_body(body, expected_type=expected_type)
