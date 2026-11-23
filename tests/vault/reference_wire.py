"""Test-only reference encodings for the vault wire protocol.

The reference implementation accepts fixture keys and deterministic inputs.
Live keys belong in the native adapter behind scoped handles.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from joserfc import jws
from joserfc.jwk import ECKey, OKPKey
from mnemonic import Mnemonic
from pyhpke import AEADId, CipherSuite, KDFId, KEMId, KEMKey, KEMKeyPair
from pyhpke.kem_primitives.x25519 import X25519 as X25519KEM

from aether.vault.errors import VaultProtocolError
from aether.vault.schema import (
    AE2_MAGIC,
    ED25519_BYTES,
    GCM_TAG_BYTES,
    HPKE_USAGES,
    ID_BYTES,
    IV_BYTES,
    KEY_BYTES,
    P256_PUBLIC_BYTES,
    PROTOCOL_VERSION,
    X25519_BYTES,
    b64u_decode,
    b64u_encode,
    canonical_bytes,
    loads_strict,
    normalize_origin,
    validate_envelope,
)

_SIGNATURE_ALGS = ("ES256", "Ed25519")


def _suite() -> CipherSuite:
    return CipherSuite.new(KEMId.DHKEM_X25519_HKDF_SHA256, KDFId.HKDF_SHA256, AEADId.AES256_GCM)


def _hpke_info(usage: str) -> bytes:
    if usage not in HPKE_USAGES:
        raise VaultProtocolError("bad_field", "unknown HPKE usage")
    return f"aether/v1/{usage}".encode("ascii")


def _kem_public(raw: bytes) -> KEMKey:
    return KEMKey.from_pyca_cryptography_key(X25519PublicKey.from_public_bytes(raw))


def _kem_private(raw: bytes) -> KEMKey:
    return KEMKey.from_pyca_cryptography_key(X25519PrivateKey.from_private_bytes(raw))


def _exact(value: Any, keys: tuple[str, ...], where: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise VaultProtocolError("bad_field", f"{where} fields are not exact")
    return value


# ---------------------------------------------------------------------------
# signatures


def signer_id(public_key: bytes) -> str:
    return b64u_encode(hashlib.sha256(public_key).digest())


def body_digest(body: Any) -> bytes:
    return hashlib.sha256(canonical_bytes(body)).digest()


def sign_body(body: Any, private_key: Any, *, alg: str, kid: str) -> dict[str, Any]:
    if alg not in _SIGNATURE_ALGS:
        raise VaultProtocolError("bad_field", "signature alg is not allowed")
    header = {"alg": alg, "kid": kid}
    protected = b64u_encode(canonical_bytes(header))
    payload = b64u_encode(canonical_bytes(body))
    signing_input = f"{protected}.{payload}".encode("ascii")
    model = jws.construct_registry([alg]).get_alg(alg)
    model.check_key(private_key)
    signature = model.sign(signing_input, private_key)
    return {"body": body, "protected": protected, "signature": b64u_encode(signature)}


def verify_body(
    envelope: Any,
    public_key: Any,
    *,
    algorithms: tuple[str, ...] = ("ES256",),
    expected_kid: str | None = None,
) -> dict[str, Any]:
    body, header, signature = validate_envelope(envelope)
    alg = header["alg"]
    if expected_kid is not None and header["kid"] != expected_kid:
        raise VaultProtocolError("bad_signature", "signer ID does not match the expected key")
    if alg not in algorithms:
        raise VaultProtocolError("bad_signature", "signature algorithm is not accepted here")
    protected = envelope["protected"]
    payload = b64u_encode(canonical_bytes(body))
    signing_input = f"{protected}.{payload}".encode("ascii")
    model = jws.construct_registry([alg]).get_alg(alg)
    if not model.verify(signing_input, signature, public_key):
        raise VaultProtocolError("bad_signature", "signature did not verify")
    return body


def ec_private_key(raw: bytes) -> ECKey:
    """Wrap a raw P-256 scalar as a JOSE signing key."""
    if len(raw) != 32:
        raise VaultProtocolError("bad_field", "P-256 private scalar must be 32 bytes")
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())
    return ECKey.import_key(key)


def ec_public_key(raw: bytes) -> ECKey:
    if len(raw) != P256_PUBLIC_BYTES or raw[0] != 4:
        raise VaultProtocolError("bad_field", "P-256 public key must be uncompressed SEC1")
    from cryptography.hazmat.primitives.asymmetric import ec

    try:
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except ValueError as exc:
        raise VaultProtocolError("bad_field", "invalid P-256 public point") from exc
    return ECKey.import_key(key)


def ed25519_private_key(seed: bytes) -> OKPKey:
    if len(seed) != ED25519_BYTES:
        raise VaultProtocolError("bad_field", "Ed25519 seed must be 32 bytes")
    key = Ed25519PrivateKey.from_private_bytes(seed)
    return OKPKey.import_key(key)


def ed25519_public_key(raw: bytes) -> OKPKey:
    if len(raw) != ED25519_BYTES:
        raise VaultProtocolError("bad_field", "Ed25519 public key must be 32 bytes")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    return OKPKey.import_key(Ed25519PublicKey.from_public_bytes(raw))


# ---------------------------------------------------------------------------
# HPKE


def hpke_seal(
    recipient_public: bytes,
    usage: str,
    header: Any,
    plaintext: bytes,
    *,
    ephemeral_private: bytes | None = None,
) -> dict[str, str]:
    if len(recipient_public) != X25519_BYTES:
        raise VaultProtocolError("bad_field", "HPKE recipient must be a 32-byte X25519 key")
    suite = _suite()
    eks: KEMKeyPair | None = None
    if ephemeral_private is not None:
        if len(ephemeral_private) != X25519_BYTES:
            raise VaultProtocolError("bad_field", "ephemeral key must be 32 bytes")
        private = X25519PrivateKey.from_private_bytes(ephemeral_private)
        eks = KEMKeyPair(
            _kem_private(ephemeral_private),
            KEMKey.from_pyca_cryptography_key(private.public_key()),
        )
    enc, context = suite.create_sender_context(
        _kem_public(recipient_public), _hpke_info(usage), eks=eks
    )
    ct = context.seal(plaintext, canonical_bytes(header))
    return {"enc": b64u_encode(enc), "ct": b64u_encode(ct)}


def hpke_open(recipient_private: bytes, usage: str, header: Any, box: Any) -> bytes:
    data = _exact(box, ("enc", "ct"), "HPKE box")
    enc = b64u_decode(data["enc"], field="enc", length=X25519_BYTES)
    ct = b64u_decode(data["ct"], field="ct")
    if len(ct) < GCM_TAG_BYTES:
        raise VaultProtocolError("bad_field", "HPKE ciphertext is too short")
    if len(recipient_private) != X25519_BYTES:
        raise VaultProtocolError("bad_field", "HPKE private key must be 32 bytes")
    context = _suite().create_recipient_context(
        enc, _kem_private(recipient_private), _hpke_info(usage)
    )
    try:
        return context.open(ct, canonical_bytes(header))
    except Exception as exc:  # pyhpke raises OpenError; never leak detail
        raise VaultProtocolError("denied", "HPKE open failed") from exc


# ---------------------------------------------------------------------------
# AE2


def derive_subkey(
    category_key: bytes, vault_id: bytes, cat: str, ver: int, fn: str, namespace: str
) -> bytes:
    if len(category_key) != KEY_BYTES:
        raise VaultProtocolError("bad_field", "category key must be 32 bytes")
    if len(vault_id) != ID_BYTES:
        raise VaultProtocolError("bad_field", "vault id must be 16 bytes")
    if fn not in ("aead", "token", "dedup"):
        raise VaultProtocolError("bad_field", "unknown subkey function")
    info = canonical_bytes(
        {"v": PROTOCOL_VERSION, "cat": cat, "ver": ver, "fn": fn, "namespace": namespace}
    )
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=vault_id, info=info).derive(
        category_key
    )


def ae2_aad(
    vault_id: bytes, cat: str, ver: int, table: str, column: str, row: str, purpose: str
) -> bytes:
    return canonical_bytes(
        {
            "v": PROTOCOL_VERSION,
            "vault": b64u_encode(vault_id),
            "cat": cat,
            "ver": ver,
            "table": table,
            "column": column,
            "row": row,
            "purpose": purpose,
        }
    )


def ae2_seal(
    category_key: bytes,
    vault_id: bytes,
    cat: str,
    ver: int,
    table: str,
    column: str,
    row: str,
    purpose: str,
    plaintext: bytes,
    *,
    nonce: bytes | None = None,
) -> bytes:
    nonce = secrets.token_bytes(IV_BYTES) if nonce is None else nonce
    if len(nonce) != IV_BYTES:
        raise VaultProtocolError("bad_field", "AE2 nonce must be 12 bytes")
    subkey = derive_subkey(category_key, vault_id, cat, ver, "aead", purpose)
    ct = AESGCM(subkey).encrypt(
        nonce, plaintext, ae2_aad(vault_id, cat, ver, table, column, row, purpose)
    )
    envelope = {
        "v": PROTOCOL_VERSION,
        "vault": b64u_encode(vault_id),
        "cat": cat,
        "ver": ver,
        "n": b64u_encode(nonce),
        "ct": b64u_encode(ct),
    }
    return AE2_MAGIC + canonical_bytes(envelope)


def ae2_open(
    category_key: bytes,
    vault_id: bytes,
    cat: str,
    ver: int,
    table: str,
    column: str,
    row: str,
    purpose: str,
    blob: bytes,
) -> bytes:
    if not isinstance(blob, bytes) or not blob.startswith(AE2_MAGIC):
        raise VaultProtocolError("bad_field", "missing AE2 envelope")
    envelope = _exact(
        loads_strict(blob[len(AE2_MAGIC) :]), ("v", "vault", "cat", "ver", "n", "ct"), "AE2"
    )
    if type(envelope["v"]) is not int or envelope["v"] != PROTOCOL_VERSION:
        raise VaultProtocolError("bad_version", "unsupported AE2 version")
    if envelope["vault"] != b64u_encode(vault_id) or envelope["cat"] != cat:
        raise VaultProtocolError("bad_field", "AE2 location does not match")
    if type(envelope["ver"]) is not int or envelope["ver"] != ver:
        raise VaultProtocolError("bad_field", "AE2 key version does not match")
    if canonical_bytes(envelope) != blob[len(AE2_MAGIC) :]:
        raise VaultProtocolError("bad_field", "AE2 envelope must use canonical JSON")
    nonce = b64u_decode(envelope["n"], field="AE2 n", length=IV_BYTES)
    ct = b64u_decode(envelope["ct"], field="AE2 ct")
    subkey = derive_subkey(category_key, vault_id, cat, ver, "aead", purpose)
    try:
        return AESGCM(subkey).decrypt(
            nonce, ct, ae2_aad(vault_id, cat, ver, table, column, row, purpose)
        )
    except InvalidTag as exc:
        raise VaultProtocolError("denied", "AE2 authentication failed") from exc


def token_value(
    category_key: bytes, vault_id: bytes, cat: str, ver: int, domain: str, value: bytes
) -> bytes:
    subkey = derive_subkey(category_key, vault_id, cat, ver, "token", domain)
    return _hmac(subkey, value)


def dedup_value(
    category_key: bytes, vault_id: bytes, cat: str, ver: int, domain: str, value: bytes
) -> bytes:
    subkey = derive_subkey(category_key, vault_id, cat, ver, "dedup", domain)
    return _hmac(subkey, value)


def _hmac(key: bytes, value: bytes) -> bytes:
    import hmac

    return hmac.new(key, value, hashlib.sha256).digest()


# ---------------------------------------------------------------------------
# recovery phrase


@dataclass(frozen=True)
class RecoveryKeys:
    entropy: bytes
    hpke_private: bytes
    hpke_public: bytes
    sign_seed: bytes
    sign_public: bytes


def phrase_from_entropy(entropy: bytes) -> str:
    if len(entropy) != 32:
        raise VaultProtocolError("bad_field", "recovery entropy must be 32 bytes")
    return Mnemonic("english").to_mnemonic(entropy)


def entropy_from_phrase(phrase: str) -> bytes:
    if not isinstance(phrase, str):
        raise VaultProtocolError("bad_field", "phrase must be a string")
    normalized = " ".join(phrase.strip().split())
    if not Mnemonic("english").check(normalized):
        raise VaultProtocolError("bad_field", "phrase checksum failed")
    return bytes(Mnemonic("english").to_entropy(normalized))


def recovery_keys(entropy: bytes, vault_id: bytes) -> RecoveryKeys:
    if len(entropy) != 32:
        raise VaultProtocolError("bad_field", "recovery entropy must be 32 bytes")
    if len(vault_id) != ID_BYTES:
        raise VaultProtocolError("bad_field", "vault id must be 16 bytes")
    hpke_ikm = HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_BYTES,
        salt=vault_id,
        info=b"aether/v1/recovery-hpke",
    ).derive(entropy)
    sign_seed = HKDF(
        algorithm=hashes.SHA256(),
        length=ED25519_BYTES,
        salt=vault_id,
        info=b"aether/v1/recovery-sign",
    ).derive(entropy)
    pair = X25519KEM().derive_key_pair(hpke_ikm, _suite().kdf)
    signer = Ed25519PrivateKey.from_private_bytes(sign_seed)
    return RecoveryKeys(
        entropy=bytes(entropy),
        hpke_private=pair.private_key.to_private_bytes(),
        hpke_public=pair.public_key.to_public_bytes(),
        sign_seed=sign_seed,
        sign_public=signer.public_key().public_bytes_raw(),
    )


# ---------------------------------------------------------------------------
# persistent wraps


def wrap_header(vault_id: bytes, cat: str, ver: int) -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "vault": b64u_encode(vault_id), "cat": cat, "ver": ver}


def device_wrap(
    master_key: bytes,
    vault_id: bytes,
    cat: str,
    ver: int,
    category_key: bytes,
    *,
    iv: bytes | None = None,
    alg: str = "android-keystore-aes-gcm",
) -> dict[str, str]:
    if alg != "android-keystore-aes-gcm":
        raise VaultProtocolError("bad_field", "software device wrap only implements Android")
    header = wrap_header(vault_id, cat, ver)
    plaintext = canonical_bytes({"header": header, "key": b64u_encode(category_key)})
    iv = secrets.token_bytes(IV_BYTES) if iv is None else iv
    if len(iv) != IV_BYTES:
        raise VaultProtocolError("bad_field", "device wrap IV must be 12 bytes")
    blob = iv + AESGCM(master_key).encrypt(iv, plaintext, canonical_bytes(header))
    return {"alg": alg, "blob": b64u_encode(blob)}


def device_unwrap(master_key: bytes, vault_id: bytes, cat: str, ver: int, wrap: Any) -> bytes:
    data = _exact(wrap, ("alg", "blob"), "device wrap")
    if data["alg"] != "android-keystore-aes-gcm":
        raise VaultProtocolError("bad_field", "unknown device wrap algorithm")
    blob = b64u_decode(data["blob"], field="device wrap blob")
    if len(blob) < IV_BYTES + GCM_TAG_BYTES:
        raise VaultProtocolError("bad_field", "device wrap is too short")
    header = wrap_header(vault_id, cat, ver)
    try:
        plaintext = AESGCM(master_key).decrypt(
            blob[:IV_BYTES], blob[IV_BYTES:], canonical_bytes(header)
        )
    except InvalidTag as exc:
        raise VaultProtocolError("denied", "device wrap authentication failed") from exc
    inner = _exact(loads_strict(plaintext), ("header", "key"), "device wrap payload")
    if inner["header"] != header:
        raise VaultProtocolError("bad_field", "device wrap header does not match")
    return b64u_decode(inner["key"], field="device wrap key", length=KEY_BYTES)


def backup_wrap(
    recovery_public: bytes,
    vault_id: bytes,
    cat: str,
    ver: int,
    category_key: bytes,
    *,
    ephemeral_private: bytes | None = None,
) -> dict[str, str]:
    header = wrap_header(vault_id, cat, ver)
    if len(category_key) != KEY_BYTES:
        raise VaultProtocolError("bad_field", "backup key has the wrong length")
    return hpke_seal(
        recovery_public, "recovery", header, category_key, ephemeral_private=ephemeral_private
    )


def backup_unwrap(recovery_private: bytes, vault_id: bytes, cat: str, ver: int, box: Any) -> bytes:
    header = wrap_header(vault_id, cat, ver)
    key = hpke_open(recovery_private, "recovery", header, box)
    if len(key) != KEY_BYTES:
        raise VaultProtocolError("bad_field", "backup key has the wrong length")
    return key


def normalize_origin_value(value: Any) -> str:
    """Small re-export so callers do not import schema for one function."""
    return normalize_origin(value)
