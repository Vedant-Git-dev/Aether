"""Pending approvals: tool calls parked for a one-tap human decision.

A REQUIRE_APPROVAL ruling never blocks the agent — the call is stored
(encrypted), the user is notified on whatever surfaces are enabled, and a
decision executes the held call. Approving one call does not approve
future calls; standing trust is expressed as a user rule in config.yaml.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from ..memory.crypto import Cipher, CryptoError
from .audit import AuditLog

log = logging.getLogger("aether.authz.approvals")

# status values
PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"
EXPIRED = "expired"
EXECUTED = "executed"


def _aad(approval_id: int) -> str:
    return f"pending_approvals:params_enc:{approval_id}"


@dataclass
class Approval:
    id: int
    tool_name: str
    params: dict[str, Any]  # decrypted
    status: str
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    decided_by: str | None


class Approvals:
    def __init__(
        self,
        pool: asyncpg.Pool,
        cipher: Cipher,
        audit: AuditLog,
        ttl_hours: float = 24.0,
    ) -> None:
        self._pool = pool
        self._cipher = cipher
        self._audit = audit
        self._ttl = timedelta(hours=ttl_hours)

    async def create(
        self,
        *,
        tool_name: str,
        params: dict[str, Any],
        actor: str = "agent",
        rules_matched: str = "",
        note: str = "",
    ) -> Approval:
        """Park a call. The row is written (params encrypted, bound to the
        row id via AAD) and an audit entry records the hold."""
        expires_at = datetime.now(timezone.utc) + self._ttl
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "INSERT INTO pending_approvals (tool_name, status, expires_at)"
                    " VALUES ($1, 'pending', $2) RETURNING id, created_at",
                    tool_name,
                    expires_at,
                )
                approval_id = row["id"]
                # encrypt only after the id exists so the AAD can bind to it
                blob = self._cipher.encrypt_json(params, aad=_aad(approval_id))
                await conn.execute(
                    "UPDATE pending_approvals SET params_enc = $1 WHERE id = $2",
                    blob,
                    approval_id,
                )
        await self._audit.append(
            actor=actor,
            tool_name=tool_name,
            decision="approve",
            rules_matched=rules_matched,
            params=params,
            outcome=note or "parked for approval",
        )
        return Approval(
            id=approval_id,
            tool_name=tool_name,
            params=params,
            status=PENDING,
            created_at=row["created_at"],
            expires_at=expires_at,
            decided_at=None,
            decided_by=None,
        )

    async def decide(
        self, approval_id: int, decision: str, decided_by: str = "user"
    ) -> Approval | None:
        """Approve or deny a still-pending, unexpired approval. Returns the
        updated Approval, or None if it was already decided or expired."""
        if decision not in (APPROVED, DENIED):
            raise ValueError(f"decision must be '{APPROVED}' or '{DENIED}', got {decision!r}")
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "UPDATE pending_approvals"
                    " SET status = $1, decided_at = now(), decision_by = $2"
                    " WHERE id = $3 AND status = 'pending' AND expires_at > now()"
                    " RETURNING id, tool_name, status, created_at, expires_at,"
                    " decided_at, decided_by",
                    decision,
                    decided_by,
                    approval_id,
                )
                if row is None:
                    return None
                blob = await conn.fetchval(
                    "SELECT params_enc FROM pending_approvals WHERE id = $1",
                    approval_id,
                )
        params = self._cipher.decrypt_json(blob, aad=_aad(approval_id))
        await self._audit.append(
            actor="user",
            tool_name=row["tool_name"],
            decision="allow" if decision == APPROVED else "deny",
            params=params,
            outcome=f"{decision} by {decided_by}",
        )
        return Approval(
            id=approval_id,
            tool_name=row["tool_name"],
            params=params,
            status=decision,
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            decided_at=row["decided_at"],
            decided_by=row["decided_by"],
        )

    async def mark_executed(self, approval_id: int) -> None:
        """Flip an approved call to executed after the tool ran."""
        row = await self._pool.fetchrow(
            "UPDATE pending_approvals SET status = 'executed'"
            " WHERE id = $1 AND status = 'approved' RETURNING tool_name",
            approval_id,
        )
        if row is None:
            return
        await self._audit.append(
            actor="agent",
            tool_name=row["tool_name"],
            decision="info",
            outcome="executed after approval",
        )

    async def expire_overdue(self) -> int:
        """Flip pending rows past their TTL to expired; audit each."""
        rows = await self._pool.fetch(
            "UPDATE pending_approvals SET status = 'expired'"
            " WHERE status = 'pending' AND expires_at <= now()"
            " RETURNING id, tool_name, params_enc"
        )
        for row in rows:
            try:
                params = self._cipher.decrypt_json(row["params_enc"], aad=_aad(row["id"]))
            except CryptoError:
                log.warning("expired approval %s had undecryptable params", row["id"])
                params = {}
            await self._audit.append(
                actor="system",
                tool_name=row["tool_name"],
                decision="info",
                params=params,
                outcome="approval expired",
            )
        return len(rows)

    async def get(self, approval_id: int) -> Approval | None:
        row = await self._pool.fetchrow(
            "SELECT id, tool_name, status, created_at, expires_at, decided_at,"
            " decided_by, params_enc FROM pending_approvals WHERE id = $1",
            approval_id,
        )
        if row is None:
            return None
        return self._to_approval(row)

    async def list_pending(self) -> list[Approval]:
        rows = await self._pool.fetch(
            "SELECT id, tool_name, status, created_at, expires_at, decided_at,"
            " decided_by, params_enc FROM pending_approvals WHERE status = 'pending'"
            " ORDER BY created_at"
        )
        approvals: list[Approval] = []
        for row in rows:
            try:
                approvals.append(self._to_approval(row))
            except CryptoError:
                log.warning("pending approval %s has undecryptable params — skipping", row["id"])
        return approvals

    def _to_approval(self, row: asyncpg.Record) -> Approval:
        params = self._cipher.decrypt_json(row["params_enc"], aad=_aad(row["id"]))
        return Approval(
            id=row["id"],
            tool_name=row["tool_name"],
            params=params,
            status=row["status"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            decided_at=row["decided_at"],
            decided_by=row["decided_by"],
        )
