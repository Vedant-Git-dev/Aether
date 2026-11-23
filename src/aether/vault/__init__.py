"""Phone-held vault protocol support.

Defines the wire schema and policy defaults for the scoped storage boundary.
The software vault test double lives under ``tests/vault``.
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
