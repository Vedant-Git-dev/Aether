"""Screen perception: a screenshot becomes a memory event, nothing more.

Perception-only by construction — this pipeline never registers an action
tool, and any action the agent later proposes from a screen memory goes
through the same authz gate as everything else.
"""

from __future__ import annotations

import base64
import logging

from ..llm.json_utils import extract_json
from ..llm.registry import ProviderRegistry
from ..llm.types import ImageBlock, Message
from ..memory.events import EventStore, IngestResult

log = logging.getLogger("aether.connectors.screenvision")

SYSTEM_PROMPT = (
    "You look at one screenshot of the user's computer and report what is on it. "
    "Perception only — never propose or take actions.\n"
    'Reply with strict JSON only: {"app": <short app or site name>, '
    '"summary": <one or two sentences>, "people": [<names or handles visible>], '
    '"commitments": [<deadlines, promises, plans visible>], '
    '"actionables": [<things that look like they need the user>]}'
)


class ScreenVision:
    """Screenshots in (the companion uploads them), encrypted memory events out."""

    def __init__(self, providers: ProviderRegistry, events: EventStore) -> None:
        self._providers = providers
        self._events = events

    async def record(self, png: bytes, note: str = "") -> IngestResult | None:
        """Describe one screenshot and store it as an event. None when no
        vision-capable provider is configured — the API layer turns that
        into a 503 so the companion can tell the user why."""
        provider = self._providers.for_role("vision")
        if provider is None or not provider.supports_vision:
            log.warning("screen capture dropped: no vision-capable provider configured")
            return None

        turn = await provider.complete(
            SYSTEM_PROMPT,
            [
                Message.user(
                    "What is on this screen?" + (f"\nUser note: {note}" if note else ""),
                    images=[
                        ImageBlock(
                            data_b64=base64.b64encode(png).decode("ascii"),
                            media_type="image/png",
                        )
                    ],
                )
            ],
            tools=[],
        )
        try:
            described = extract_json(turn.text)
            if not isinstance(described, dict):
                raise ValueError("judge returned a non-object")
        except Exception:
            # a vision turn that didn't produce JSON still carries information —
            # keep the raw text rather than lose the capture
            log.warning("screen vision returned non-JSON; storing raw text")
            described = {"summary": (turn.text or "")[:1000]}

        payload = {k: v for k, v in described.items() if v not in (None, "", [])}
        if note:
            payload["user_note"] = note
        return await self._events.ingest(
            source="screen", kind="screen_capture", payload=payload
        )
