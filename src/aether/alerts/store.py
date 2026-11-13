"""Alerts at rest: sealed specs, sealed provenance, owner-only writes.

The row's plaintext columns are routing metadata only: a keyed blind token
over the scope, the status word, the event watermark, a conversation tag for
migrated routines, and storage timestamps. The title, every criterion, the
optional action and the provenance that allowed the write are sealed and
bound to the row through the cipher's AAD.

A write is refused unless the turn it rode says it came from the owner's own
message or an approved card. That check is structural — `TurnOrigin` is built
by the harness — so a mail body, a web page or a tool result can never arm,
change or remove one, however it is worded.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import asyncpg

from ..authz.audit import AuditLog
from ..memory.crypto import Cipher
from ..origin import TurnOrigin
from .spec import ACTIVE, PAUSED, Alert, AlertSpec, has_criteria, normalize_spec

log = logging.getLogger("aether.alerts")

# The AAD prefix is a storage detail, frozen when the table was first created
# as `watches`. Old rows still open under it; every new write uses `alerts`.
_SPEC_AAD = "alerts:spec_enc:{}"
_LEGACY_SPEC_AAD = "watches:spec_enc:{}"
_ORIGIN_AAD = "alerts:origin_enc:{}"

_COLUMNS = (
    "id, status, spec_enc, channel, converted_from, last_event_id,"
    " last_run_at, last_notified_at, created_at"
)


def _clean(value: Any, limit: int = 200) -> str:
    return str(value or "").strip()[:limit]


class AlertStore:
    """Database-backed alerts, sealed specs, provenance-gated writes."""

    def __init__(self, pool: asyncpg.Pool, cipher: Cipher, audit: AuditLog) -> None:
        self._pool = pool
        self._cipher = cipher
        self._audit = audit

    async def create(
        self,
        *,
        spec: AlertSpec,
        origin: TurnOrigin,
        actor: str = "owner",
        converted_from: str = "",
    ) -> Alert | None:
        """Arm one alert. A turn without owner or approved provenance is
        refused outright — the deterministic gate that keeps content from
        arming one.

        Creating the same condition twice returns the existing row instead of
        a duplicate, so a repeated "keep checking" is one alert.
        """
        if not origin.may_mutate:
            log.warning("refused an alert write from a %s origin", origin.kind)
            return None
        spec = normalize_spec(spec)
        if not has_criteria(spec):
            return None
        for existing in await self.list_active():
            if existing.equivalent(spec):
                return existing
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO alerts (scope_token, channel, status, spec_enc,"
                " converted_from, key_version)"
                " VALUES ($1, $2, $3, $4, $5, $6) RETURNING id, created_at",
                self._cipher.blind_token("alert_scope", spec.scope),
                spec.channel,
                ACTIVE,
                b"",  # placeholder until the id exists; replaced below, same transaction
                converted_from,
                self._cipher.key_id,
            )
            alert_id = row["id"]
            await conn.execute(
                "UPDATE alerts SET spec_enc = $1, origin_enc = $2 WHERE id = $3",
                self._cipher.encrypt_json(spec.to_payload(), aad=_SPEC_AAD.format(alert_id)),
                self._cipher.encrypt_json(origin.payload(), aad=_ORIGIN_AAD.format(alert_id)),
                alert_id,
            )
        await self._audit.append(
            actor=actor,
            tool_name="alert",
            decision="info",
            params={"alert_id": alert_id, "scope": spec.scope, "style": spec.style},
            outcome="alert armed",
        )
        return Alert(
            id=alert_id,
            spec=spec,
            status=ACTIVE,
            last_event_id=0,
            last_run_at=None,
            last_notified_at=None,
            created_at=row["created_at"],
            converted_from=converted_from,
        )

    async def import_alert(
        self, *, spec: AlertSpec, converted_from: str, reference: dict[str, Any]
    ) -> Alert | None:
        """One row moved in by a migration, not by a turn.

        The migration reference is recorded as provenance, so the row says
        plainly where it came from. Idempotent: a second import of the same
        source is a no-op."""
        if converted_from:
            existing = await self._pool.fetchval(
                "SELECT id FROM alerts WHERE converted_from = $1", converted_from
            )
            if existing is not None:
                return await self.get(int(existing))
        spec = normalize_spec(spec)
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO alerts (scope_token, channel, status, spec_enc,"
                " converted_from, key_version)"
                " VALUES ($1, $2, $3, $4, $5, $6)"
                " ON CONFLICT (converted_from) WHERE converted_from IS NOT NULL"
                " DO NOTHING RETURNING id, created_at",
                self._cipher.blind_token("alert_scope", spec.scope),
                spec.channel,
                ACTIVE,
                b"",
                converted_from,
                self._cipher.key_id,
            )
            if row is None:
                return None
            alert_id = int(row["id"])
            await conn.execute(
                "UPDATE alerts SET spec_enc = $1, origin_enc = $2 WHERE id = $3",
                self._cipher.encrypt_json(spec.to_payload(), aad=_SPEC_AAD.format(alert_id)),
                self._cipher.encrypt_json(
                    {"kind": "migration", "reference": dict(reference)},
                    aad=_ORIGIN_AAD.format(alert_id),
                ),
                alert_id,
            )
        return Alert(
            id=alert_id,
            spec=spec,
            status=ACTIVE,
            last_event_id=0,
            last_run_at=None,
            last_notified_at=None,
            created_at=row["created_at"],
            converted_from=converted_from,
        )

    async def list(self, limit: int = 50) -> list[Alert]:
        rows = await self._pool.fetch(
            f"SELECT {_COLUMNS} FROM alerts ORDER BY id DESC LIMIT $1", limit
        )
        return [self._to_alert(row) for row in rows]

    async def list_active(self) -> list[Alert]:
        rows = await self._pool.fetch(
            f"SELECT {_COLUMNS} FROM alerts WHERE status = $1 ORDER BY id", ACTIVE
        )
        return [self._to_alert(row) for row in rows]

    async def active_count(self) -> int:
        value = await self._pool.fetchval("SELECT count(*) FROM alerts WHERE status = $1", ACTIVE)
        return int(value or 0)

    async def get(self, alert_id: int) -> Alert | None:
        row = await self._pool.fetchrow(f"SELECT {_COLUMNS} FROM alerts WHERE id = $1", alert_id)
        return self._to_alert(row) if row else None

    async def set_enabled(
        self, alert_id: int, enabled: bool, *, origin: TurnOrigin
    ) -> Alert | None:
        if not origin.may_mutate:
            log.warning("refused an alert change from a %s origin", origin.kind)
            return None
        status = ACTIVE if enabled else PAUSED
        row = await self._pool.fetchrow(
            f"UPDATE alerts SET status = $1 WHERE id = $2 RETURNING {_COLUMNS}",
            status,
            alert_id,
        )
        if row is None:
            return None
        alert = self._to_alert(row)
        await self._audit.append(
            actor="owner",
            tool_name="alert",
            decision="info",
            params={"alert_id": alert_id, "status": status},
            outcome="alert resumed" if enabled else "alert paused",
        )
        return alert

    async def update(self, alert_id: int, *, spec: AlertSpec, origin: TurnOrigin) -> Alert | None:
        """Replace one alert's stored condition. The id and its watermark
        stay; only the sealed spec and the plaintext routing columns move."""
        if not origin.may_mutate:
            log.warning("refused an alert change from a %s origin", origin.kind)
            return None
        spec = normalize_spec(spec)
        row = await self._pool.fetchrow(
            "UPDATE alerts SET spec_enc = $1, channel = $2, scope_token = $3,"
            f" origin_enc = $4 WHERE id = $5 RETURNING {_COLUMNS}",
            self._cipher.encrypt_json(spec.to_payload(), aad=_SPEC_AAD.format(alert_id)),
            spec.channel,
            self._cipher.blind_token("alert_scope", spec.scope),
            self._cipher.encrypt_json(origin.payload(), aad=_ORIGIN_AAD.format(alert_id)),
            alert_id,
        )
        if row is None:
            return None
        await self._audit.append(
            actor="owner",
            tool_name="alert",
            decision="info",
            params={"alert_id": alert_id, "scope": spec.scope, "style": spec.style},
            outcome="alert updated",
        )
        return self._to_alert(row)

    async def delete(self, alert_id: int, *, origin: TurnOrigin) -> bool:
        if not origin.may_mutate:
            log.warning("refused an alert delete from a %s origin", origin.kind)
            return False
        row = await self._pool.fetchrow("DELETE FROM alerts WHERE id = $1 RETURNING id", alert_id)
        if row is not None:
            await self._audit.append(
                actor="owner",
                tool_name="alert",
                decision="info",
                params={"alert_id": alert_id},
                outcome="alert removed",
            )
        return row is not None

    async def mark_run(self, alert_ids: list[int], last_event_id: int) -> None:
        """Advance the id watermark for every alert that evaluated this
        event, in one statement. The conditional keeps a concurrent later
        write from being rolled backwards."""
        if not alert_ids:
            return
        await self._pool.execute(
            "UPDATE alerts SET last_event_id = $2 WHERE id = ANY($1) AND last_event_id < $2",
            alert_ids,
            last_event_id,
        )

    async def mark_action_fired(self, alert_id: int) -> None:
        """The action's own clock — a separate stamp from the event
        watermark, so a burst of matching events runs the action once."""
        await self._pool.execute("UPDATE alerts SET last_run_at = now() WHERE id = $1", alert_id)

    async def complete(self, alert_id: int) -> None:
        """A one-shot reminder that has been delivered stops being armed."""
        row = await self._pool.fetchrow(
            "UPDATE alerts SET status = $1 WHERE id = $2 AND status = $3 RETURNING id",
            PAUSED,
            alert_id,
            ACTIVE,
        )
        if row is not None:
            await self._audit.append(
                actor="alert",
                tool_name="alert",
                decision="info",
                params={"alert_id": alert_id},
                outcome="reminder delivered",
            )

    async def mark_notified(self, alert_ids: list[int]) -> None:
        if not alert_ids:
            return
        await self._pool.execute(
            "UPDATE alerts SET last_notified_at = now() WHERE id = ANY($1)", alert_ids
        )

    def _to_alert(self, row: asyncpg.Record) -> Alert:
        spec = AlertSpec()
        alert_id = int(row["id"])
        for aad in (_SPEC_AAD.format(alert_id), _LEGACY_SPEC_AAD.format(alert_id)):
            try:
                spec = AlertSpec.from_payload(self._cipher.decrypt_json(row["spec_enc"], aad=aad))
                break
            except Exception:
                continue
        else:
            log.warning("alert %s spec undecryptable — treated as empty", alert_id)
        return Alert(
            id=alert_id,
            spec=spec,
            status=row["status"],
            last_event_id=int(row["last_event_id"] or 0),
            last_run_at=row["last_run_at"],
            last_notified_at=row["last_notified_at"],
            created_at=row["created_at"],
            converted_from=str(row["converted_from"] or ""),
        )


def where_due(alert: Alert, now: datetime) -> bool:
    """A reminder is due when its sealed moment has arrived; an event alert
    is never 'due' on the clock."""
    if not alert.spec.is_reminder or not alert.spec.remind_at:
        return False
    try:
        moment = datetime.fromisoformat(alert.spec.remind_at)
    except ValueError:
        return False
    if moment.tzinfo is None:
        return moment <= now.replace(tzinfo=None)
    return moment <= now
