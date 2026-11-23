"""Phone-held vault protocol support.

Phase 1a defines the wire schema and policy defaults.
Later phases add the scoped storage boundary, migrations, audit payloads and
the migration tooling. Nothing in this package ships a software vault; the
test double lives under ``tests/vault``.
"""

from __future__ import annotations

from .errors import (
    DENIAL_REASONS,
    ERROR_CODES,
    VAULT_CATALOG_KEYS,
    VaultProtocolError,
)
from .policy import VaultPolicy, policy
from .schema import (
    CATEGORIES,
    MESSAGE_TYPES,
    OPERATIONS,
    PATHS,
    PROTOCOL_VERSION,
    USE_CASES,
    validate_body,
    validate_envelope,
    vault_path,
)

__all__ = [
    "CATEGORIES",
    "DENIAL_REASONS",
    "ERROR_CODES",
    "MESSAGE_TYPES",
    "OPERATIONS",
    "PATHS",
    "PROTOCOL_VERSION",
    "USE_CASES",
    "VAULT_CATALOG_KEYS",
    "VaultPolicy",
    "VaultProtocolError",
    "policy",
    "validate_body",
    "validate_envelope",
    "vault_path",
]
