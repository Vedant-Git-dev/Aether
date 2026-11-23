"""The closed error, denial and status vocabulary for the vault protocol.

Every HTTP failure returns only ``{code}`` from this catalog. The catalog is
part of the wire version: adding a code is a schema change. Denial reasons on
``lease_denied`` are a separate, smaller fixed enum. User-facing sentences live
in the message catalog under the keys mapped here; nothing in this module
invents words.
"""

from __future__ import annotations

# HTTP status for each closed code.
ERROR_STATUS: dict[str, int] = {
    "malformed": 400,
    "bad_version": 400,
    "duplicate_field": 400,
    "bad_field": 400,
    "unauthorized": 401,
    "bad_signature": 401,
    "denied": 403,
    "bad_scope": 403,
    "conflict": 409,
    "replay": 409,
    "stale_generation": 409,
    "stale_manifest": 409,
    "expired_challenge": 410,
    "expired_request": 410,
    "locked": 423,
    "category_unavailable": 423,
    "rate_limited": 429,
    "unavailable": 503,
    "transient": 503,
    "unpaired": 503,
}

ERROR_CODES = frozenset(ERROR_STATUS)

# Fixed reasons a phone may put on a signed denial.
DENIAL_REASONS = (
    "bad_scope",
    "locked",
    "manifest",
    "rate",
    "stale",
    "unavailable",
    "user_required",
)

VAULT_STATES = ("available", "locked")

# Status surfaces add two local, non-wire states the panel may show.
DISPLAY_STATES = ("available", "locked", "offline", "delayed")

ERROR_CATALOG_KEYS: dict[str, str] = {code: f"vault.error.{code}" for code in sorted(ERROR_CODES)}
DENIAL_CATALOG_KEYS: dict[str, str] = {
    reason: f"vault.denied.{reason}" for reason in DENIAL_REASONS
}
STATUS_CATALOG_KEYS: dict[str, str] = {state: f"vault.status.{state}" for state in DISPLAY_STATES}

# Every key this module promises the message catalog contains.
VAULT_CATALOG_KEYS = frozenset(
    (*ERROR_CATALOG_KEYS.values(), *DENIAL_CATALOG_KEYS.values(), *STATUS_CATALOG_KEYS.values())
)


class VaultProtocolError(ValueError):
    """A rejection that maps to one closed error code.

    The detail is for logs and tests only. ``payload`` is what a route may
    return: the code and nothing that could identify the rejected input.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown vault error code {code!r}")
        super().__init__(detail or code)
        self.code = code
        self.detail = detail

    @property
    def status(self) -> int:
        return ERROR_STATUS[self.code]

    def payload(self) -> dict[str, str]:
        return {"code": self.code}
