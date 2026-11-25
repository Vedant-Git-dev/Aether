"""Pending approvals: tool calls parked for a one-tap human decision.

A REQUIRE_APPROVAL ruling never blocks the agent — the call is stored
(encrypted), the user is notified on whatever surfaces are enabled, and a
decision executes the held call. Approving one call does not approve
future calls; standing trust is expressed as a user rule in config.yaml.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from ..memory.crypto import Cipher, CryptoError
from ..memory.rows import CipherRows, Rows, ScopedRows
from ..vault.scope import KeyScope
from .audit import AuditLog

log = logging.getLogger("aether.authz.approvals")

# status values
PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"
EXPIRED = "expired"
EXECUTED = "executed"
FAILED = "failed"


def _aad(approval_id: int) -> str:
    return f"pending_approvals:params_enc:{approval_id}"


def _sealed_text(rows: Rows, table: str, column: str, row_id: Any, blob: Any, aad: str) -> str:
    """A sealed string, empty when the row has none or it no longer opens."""
    if blob:
        try:
            return rows.open_bytes(blob, table=table, column=column, row_id=row_id, aad=aad).decode(
                "utf-8"
            )
        except CryptoError:
            log.warning("approval field undecryptable — treated as empty")
    return ""


_AAD_RULES = "pending_approvals:rules_matched_enc:{}"
_AAD_NOTE = "pending_approvals:note_enc:{}"


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
    # why the gate parked this call — carried on the row so the panel can
    # show the reason, not just the ask (empty for rows that predate 007)
    rules_matched: str = ""
    note: str = ""


class Approvals:
    def __init__(
        self,
        pool: asyncpg.Pool,
        cipher: Cipher | None,
        audit: AuditLog,
        ttl_hours: float = 24.0,
        *,
        rows: Rows | None = None,
    ) -> None:
        if (cipher is None) == (rows is None):
            raise ValueError("Approvals needs exactly one of cipher or rows")
        self._rows: Rows = rows if rows is not None else CipherRows(cipher)
        self._scoped = self._rows.scoped
        self._pool = pool
        self._audit = audit
        self._ttl = timedelta(hours=ttl_hours)

    @classmethod
    def scoped(
        cls,
        pool: asyncpg.Pool,
        scope: KeyScope,
        audit: AuditLog,
        ttl_hours: float = 24.0,
        *,
        ver: int = 1,
    ) -> Approvals:
        """Explicit scoped construction. The caller owns the task and scope."""
        return cls(
            pool,
            None,
            audit,
            ttl_hours,
            rows=ScopedRows(scope, cat="operations", ver=ver),
        )

    def set_ttl(self, hours: float) -> None:
        """Live-update the approval window — a chat-made change to
        authz.approval_ttl_hours takes effect on the next parked call."""
        self._ttl = timedelta(hours=hours)

    async def create(
        self,
        *,
        tool_name: str,
        params: dict[str, Any],
        actor: str = "agent",
        rules_matched: str = "",
        note: str = "",
        media_id: int | None = None,
    ) -> Approval:
        """Park a call. The row is written (params encrypted, bound to the
        row id via AAD) and an audit entry records the hold. `media_id`
        stays supported for rows parked before media-carrying calls began
        being refused at the gate — those legacy references keep their
        blobs safe from eviction while their questions still wait."""
        self._rows.preflight()
        expires_at = datetime.now(UTC) + self._ttl
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO pending_approvals"
                " (tool_name, params_enc, status, expires_at, media_id, key_version)"
                " VALUES ($1, $2, 'pending', $3, $4, $5) RETURNING id, created_at",
                tool_name,
                b"",  # placeholder until the id exists; replaced below, same transaction
                expires_at,
                media_id,
                self._rows.key_version,
            )
            approval_id = row["id"]
            # encrypt only after the id exists so the AAD can bind to it
            blob = self._rows.seal(
                params,
                table="pending_approvals",
                column="params_enc",
                row_id=approval_id,
                aad=_aad(approval_id),
            )
            await conn.execute(
                "UPDATE pending_approvals SET params_enc = $1, rules_matched_enc = $2,"
                " note_enc = $3 WHERE id = $4",
                blob,
                self._rows.seal_bytes(
                    rules_matched.encode("utf-8"),
                    table="pending_approvals",
                    column="rules_matched_enc",
                    row_id=approval_id,
                    aad=_AAD_RULES.format(approval_id),
                ),
                self._rows.seal_bytes(
                    note.encode("utf-8"),
                    table="pending_approvals",
                    column="note_enc",
                    row_id=approval_id,
                    aad=_AAD_NOTE.format(approval_id),
                ),
                approval_id,
            )
        # the audit record keeps ids and digests only: the free-text note
        # already lives, sealed, on the approval row itself, and the rule is
        # a policy vocabulary id rather than user content
        await self._audit.append(
            actor=actor,
            tool_name=tool_name,
            decision="approve",
            rules_matched=rules_matched,
            params=params,
            outcome="parked for approval",
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
            rules_matched=rules_matched,
            note=note,
        )

    async def decide(
        self, approval_id: int, decision: str, decided_by: str = "user"
    ) -> Approval | None:
        """Approve or deny a still-pending, unexpired approval. Returns the
        updated Approval, or None if it was already decided or expired."""
        self._rows.preflight()
        if decision not in (APPROVED, DENIED):
            raise ValueError(f"decision must be '{APPROVED}' or '{DENIED}', got {decision!r}")
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                # the media reference clears here too — hygiene only: the
                # eviction query filters by pending status, so protection
                # disengages with the status change even if this clear
                # were ever missed
                "UPDATE pending_approvals"
                " SET status = $1, decided_at = now(), decided_by = $2, media_id = NULL"
                " WHERE id = $3 AND status = 'pending' AND expires_at > now()"
                " RETURNING id, tool_name, status, created_at, expires_at,"
                " decided_at, decided_by, rules_matched_enc, note_enc, params_enc",
                decision,
                decided_by,
                approval_id,
            )
            if row is None:
                return None
            approval = self._to_approval(row)
        params = approval.params
        await self._audit.append(
            actor="user",
            tool_name=row["tool_name"],
            decision="allow" if decision == APPROVED else "deny",
            params=params,
            outcome=f"{decision} by {decided_by}",
        )
        log.info("approval %s decided %s by %s", approval_id, decision, decided_by)
        return approval

    async def mark_executed(self, approval_id: int) -> None:
        """Flip an approved call to executed after the tool ran."""
        self._rows.preflight()
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

    async def fail_pending_matching(self, tool_prefix: str, reason: str) -> int:
        """Fail every still-pending approval for one app. Used when a
        provider connection is revoked or needs reconnecting: an approval
        for a tool that no longer exists must not keep offering a run."""
        self._rows.preflight()
        rows = await self._pool.fetch(
            "UPDATE pending_approvals SET status = 'failed', media_id = NULL"
            " WHERE status = 'pending' AND left(tool_name, length($1)) = $1"
            " RETURNING id, tool_name",
            tool_prefix,
        )
        for row in rows:
            await self._audit.append(
                actor="system",
                tool_name=row["tool_name"],
                decision="info",
                outcome=reason,
            )
        return len(rows)

    async def mark_failed(self, approval_id: int) -> None:
        """Flip an approved call to failed — the carried-out run couldn't
        happen, and a broken approval must not linger as if it still owed
        the user a run."""
        self._rows.preflight()
        row = await self._pool.fetchrow(
            "UPDATE pending_approvals SET status = 'failed'"
            " WHERE id = $1 AND status = 'approved' RETURNING tool_name",
            approval_id,
        )
        if row is None:
            return
        await self._audit.append(
            actor="agent",
            tool_name=row["tool_name"],
            decision="info",
            outcome="could not run after approval",
        )

    async def expire_overdue(self) -> int:
        """Flip pending rows past their TTL to expired; audit each."""
        self._rows.preflight()
        rows = await self._pool.fetch(
            "UPDATE pending_approvals SET status = 'expired'"
            " WHERE status = 'pending' AND expires_at <= now()"
            " RETURNING id, tool_name, params_enc"
        )
        for row in rows:
            try:
                params = self._rows.open(
                    row["params_enc"],
                    table="pending_approvals",
                    column="params_enc",
                    row_id=row["id"],
                    aad=_aad(row["id"]),
                )
            except CryptoError:
                if self._scoped:
                    raise
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
        self._rows.preflight()
        row = await self._pool.fetchrow(
            "SELECT id, tool_name, status, created_at, expires_at, decided_at,"
            " decided_by, rules_matched_enc, note_enc, params_enc"
            " FROM pending_approvals WHERE id = $1",
            approval_id,
        )
        if row is None:
            return None
        return self._to_approval(row)

    async def find_pending(self, tool_name: str, params: dict[str, Any]) -> Approval | None:
        """The still-pending approval for this exact call, if one exists —
        the park path asks the user once per request, never twice."""
        self._rows.preflight()
        for approval in await self.list_pending():
            if approval.tool_name == tool_name and approval.params == params:
                return approval
        return None

    async def list_pending(self, limit: int | None = None, offset: int = 0) -> list[Approval]:
        self._rows.preflight()
        query = (
            "SELECT id, tool_name, status, created_at, expires_at, decided_at,"
            " decided_by, rules_matched_enc, note_enc, params_enc"
            " FROM pending_approvals WHERE status = 'pending' AND expires_at > now()"
            " ORDER BY created_at"
        )
        if limit is None:
            rows = await self._pool.fetch(query)
        else:
            rows = await self._pool.fetch(query + " LIMIT $1 OFFSET $2", limit, max(0, offset))
        approvals: list[Approval] = []
        for row in rows:
            try:
                approvals.append(self._to_approval(row))
            except CryptoError:
                log.warning("pending approval %s has undecryptable params — skipping", row["id"])
        return approvals

    def _to_approval(self, row: asyncpg.Record) -> Approval:
        params = self._rows.open(
            row["params_enc"],
            table="pending_approvals",
            column="params_enc",
            row_id=row["id"],
            aad=_aad(row["id"]),
        )
        return Approval(
            id=row["id"],
            tool_name=row["tool_name"],
            params=params,
            status=row["status"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            decided_at=row["decided_at"],
            decided_by=row["decided_by"],
            rules_matched=_sealed_text(
                self._rows,
                "pending_approvals",
                "rules_matched_enc",
                row["id"],
                row.get("rules_matched_enc"),
                _AAD_RULES.format(row["id"]),
            ),
            note=_sealed_text(
                self._rows,
                "pending_approvals",
                "note_enc",
                row["id"],
                row.get("note_enc"),
                _AAD_NOTE.format(row["id"]),
            ),
        )
