"""One-time conversions the SQL migrations cannot do.

Encryption is the reason: a routine's trigger, action and label are sealed
blobs whose AAD names the routines table, so only Python holding the key can
read them and write the equivalent sealed alert. The conversion is
idempotent — the source row id is recorded on the alert it produced — so a
crash mid-way resumes instead of duplicating.

The tables themselves are renamed by `026_alerts.sql` (`watches` → `alerts`,
`routines` → `routines_legacy`); this module only moves rows.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from ..memory.crypto import Cipher
from .spec import ALERT, AlertSpec
from .store import AlertStore

log = logging.getLogger("aether.alerts")


def _aad(routine_id: int, column: str) -> str:
    return f"routines:{column}:{routine_id}"


def _label(cipher: Cipher, row: asyncpg.Record) -> str:
    blob = row.get("label_enc")
    if not blob:
        return ""
    try:
        return cipher.decrypt_text(blob, aad=f"routines:label_enc:{row['id']}")
    except Exception:
        log.warning("routine %s label undecryptable — migrated without one", row["id"])
        return ""


def _json(cipher: Cipher, blob: Any, aad: str, label: str) -> dict[str, Any]:
    if not blob:
        return {}
    try:
        data = cipher.decrypt_json(blob, aad=aad)
    except Exception:
        log.warning("routine %s %s undecryptable — migrated empty", label, aad)
        return {}
    return data if isinstance(data, dict) else {}


async def routines_to_alerts(pool: asyncpg.Pool, cipher: Cipher, store: AlertStore) -> int:
    """Turn every legacy routine into an alert with an optional action.

    A routine whose trigger names no condition cannot be represented as an
    alert (an alert that matches everything is refused), so it is left in
    `routines_legacy` and counted in the log instead of guessed at."""
    exists = await pool.fetchval("SELECT to_regclass('public.routines_legacy')")
    if exists is None:
        return 0
    rows = await pool.fetch(
        "SELECT id, label_enc, trigger_enc, action_enc FROM routines_legacy ORDER BY id"
    )
    moved = 0
    skipped = 0
    for row in rows:
        routine_id = int(row["id"])
        trigger = _json(cipher, row["trigger_enc"], _aad(routine_id, "trigger_enc"), "trigger")
        action = _json(cipher, row["action_enc"], _aad(routine_id, "action_enc"), "action")
        source = str(trigger.get("source") or "").strip()
        condition = str(trigger.get("contains") or "").strip()
        sender = str(trigger.get("from") or "").strip()
        event = str(trigger.get("kind") or "").strip()
        if source in ("", "*"):
            source = ""
        spec = AlertSpec(
            title=_label(cipher, row),
            kind=ALERT,
            scope=source,
            event="" if event in ("", "*") else event,
            sender=sender,
            contains=condition,
            action=(
                {"tool": str(action.get("tool") or ""), "params": dict(action.get("params") or {})}
                if str(action.get("type") or "tool") == "tool" and action.get("tool")
                else None
            ),
        )
        if not (spec.scope or spec.event or spec.sender or spec.contains or spec.action):
            skipped += 1
            continue
        made = await store.import_alert(
            spec=spec,
            converted_from=f"routine:{routine_id}",
            reference={"routine_id": routine_id},
        )
        if made is not None:
            moved += 1
    if moved or skipped:
        log.info("routine migration: %d moved to alerts, %d skipped", moved, skipped)
    return moved
