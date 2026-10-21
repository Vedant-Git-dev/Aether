"""A durable ledger of which platform updates were already processed.

Long polling's offset lives only in memory: a restarted process or a
network retry re-delivers the same update, and the agent answers twice.
The row is the once-only word — the first sighting wins, every later
delivery is a duplicate and is dropped by the connector.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

log = logging.getLogger("aether.connectors.processed")

# The window a duplicate can still arrive inside. The prune runs before
# each insert, so an update older than the window is judged as unseen —
# the ledger forgot it, exactly as if it had never been recorded.
_PRUNE_OLDER_THAN = "DELETE FROM processed_updates WHERE created_at < now() - interval '30 days'"


class ProcessedUpdates:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def record(self, source: str, update_id: int) -> bool:
        """True when this update was never seen before.

        The insert is atomic — concurrent sightings of one update agree
        on exactly one winner. The prune is best-effort: its failure
        never costs a real message, it only leaves old rows behind.
        """
        async with self._pool.acquire() as conn:
            with contextlib.suppress(Exception):
                await conn.execute(_PRUNE_OLDER_THAN)
            row = await conn.fetchrow(
                "INSERT INTO processed_updates (source, update_id) VALUES ($1, $2) "
                "ON CONFLICT (source, update_id) DO NOTHING RETURNING id",
                source,
                update_id,
            )
            return row is not None
