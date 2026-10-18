"""One-time transfer URLs — the only bridge between a batch job's runner
and Aether.

A batch job doesn't stream: Aether hands the runner two unguessable,
short-lived URLs — one to pull its files from, one to push its result
back to — and the URLs are the entire authentication. No API token
crosses into the repo, no job content rides along in the dispatch, and
each URL works exactly once: the bundle read or the result write marks
the row used in the same statement that serves it, so a replayed URL
is simply dead. The token never touches the database — only its
sha256 — and the payload rides as an AES-GCM blob bound to that hash,
the same pattern every other store uses.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ..memory.crypto import Cipher

log = logging.getLogger("aether.workbench.transfers")

BUNDLE = "bundle"  # GET — the job's files, served once
RESULT = "result"  # POST — the job's outcome, accepted once
KINDS = frozenset({BUNDLE, RESULT})


class TransferError(ValueError):
    """A kind or payload the transfer vocabulary refuses."""


@dataclass(frozen=True)
class TransferTicket:
    kind: str
    token: str  # lives only in the URL minted for one job — shown once
    expires_at: datetime


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def _aad(token_hash: bytes) -> str:
    # bound to the row's own token hash — unique, and known before insert
    return f"workbench:transfer:{token_hash.hex()}"


class TransferStore:
    """Single-use transfers in Postgres — one atomic statement per use."""

    def __init__(self, pool: Any, cipher: Cipher) -> None:
        self._pool = pool
        self._cipher = cipher

    async def mint(self, *, kind: str, payload: dict, ttl_seconds: int) -> TransferTicket:
        """One fresh transfer: a random 32-byte token, its hash on the
        row, the payload encrypted against that hash."""
        if kind not in KINDS:
            raise TransferError(f"unknown transfer kind {kind!r}")
        token = secrets.token_urlsafe(32)
        token_hash = _digest(token)
        blob = self._cipher.encrypt_json(payload, aad=_aad(token_hash))
        expires_at = datetime.now() + timedelta(seconds=ttl_seconds)
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO workbench_transfers (token_hash, kind, payload_enc, expires_at)"
                " VALUES ($1, $2, $3, $4)",
                token_hash,
                kind,
                blob,
                expires_at,
            )
        return TransferTicket(kind=kind, token=token, expires_at=expires_at)

    async def take_bundle(self, token: str) -> dict[str, Any] | None:
        """Serve the bundle exactly once: the same statement that hands
        the payload over marks it used. None when the URL is unknown,
        already spent, or past its minutes."""
        row = await self._pool.fetchrow(
            "UPDATE workbench_transfers SET used = true"
            " WHERE token_hash = $1 AND kind = 'bundle' AND used = false AND expires_at > now()"
            " RETURNING payload_enc",
            _digest(token),
        )
        if row is None:
            return None
        return self._cipher.decrypt_json(row["payload_enc"], aad=_aad(_digest(token)))

    async def post_result(self, token: str, result: dict[str, Any]) -> bool:
        """Accept the job's outcome exactly once, into the row the job's
        own result URL names. False when the URL is unknown, spent, or
        expired — the runner's answer to that is to give up, not retry."""
        blob = self._cipher.encrypt_json(result, aad=_aad(_digest(token)))
        row = await self._pool.fetchrow(
            "UPDATE workbench_transfers SET payload_enc = $2, used = true"
            " WHERE token_hash = $1 AND kind = 'result' AND used = false AND expires_at > now()"
            " RETURNING id",
            _digest(token),
            blob,
        )
        return row is not None

    async def collect_result(self, token: str) -> dict[str, Any] | None:
        """The posted outcome for one result URL — None until the runner
        has posted it (or ever, if it never did)."""
        row = await self._pool.fetchrow(
            "SELECT payload_enc FROM workbench_transfers"
            " WHERE token_hash = $1 AND kind = 'result' AND used = true",
            _digest(token),
        )
        if row is None:
            return None
        return self._cipher.decrypt_json(row["payload_enc"], aad=_aad(_digest(token)))

    async def drop_expired(self) -> None:
        """Best-effort prune of dead rows — a failed prune never fails
        the caller that triggered it."""
        try:
            await self._pool.execute("DELETE FROM workbench_transfers WHERE expires_at < now()")
        except Exception:  # noqa: BLE001 — house rule: never crash on cleanup
            log.warning("expired transfer prune failed", exc_info=True)
