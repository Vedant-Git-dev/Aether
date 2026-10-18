"""Voice notes to words — Groq's free, cardless Whisper endpoint.

The key is resolved fresh per note (paste store, environment, .env — the
resolver's usual precedence), so a pasted GROQ_API_KEY applies without a
restart and an unset one leaves voice notes as kept-but-unread bytes.
Transcription never raises on the caller's path: no key, no answer, or a
broken reply all come back as None, and the caller degrades to the plain
note.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("aether.media.voice")

# Groq's OpenAI-compatible audio endpoint — whisper-large-v3, free tier
_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class GroqTranscriber:
    """whisper-large-v3 through Groq. The resolver is the one that turns
    GROQ_API_KEY into a value on demand; the client is injectable so
    tests ride an httpx MockTransport instead of the network."""

    model = "whisper-large-v3"

    def __init__(self, resolver: Any, client: httpx.AsyncClient | None = None) -> None:
        self._resolver = resolver
        self._client = client

    async def transcribe(self, data: bytes, filename: str, media_type: str = "") -> str | None:
        """The spoken words, or None when transcription can't happen — a
        missing key, a failed request, or an empty answer. Never raises."""
        key = await self._resolver.resolve("GROQ_API_KEY")
        if not key:
            return None
        client = self._client or httpx.AsyncClient(timeout=_TIMEOUT)
        try:
            response = await client.post(
                _URL,
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (filename, data, media_type or "application/octet-stream")},
                data={"model": self.model, "response_format": "json"},
            )
            response.raise_for_status()
            text = (response.json().get("text") or "").strip()
            return text or None
        except Exception:
            log.warning(
                "transcription of %s failed — the note stays unread", filename, exc_info=True
            )
            return None
        finally:
            if self._client is None:
                await client.aclose()
