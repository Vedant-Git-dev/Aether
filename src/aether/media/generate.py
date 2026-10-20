"""Image generation — the keyless default and the keyed quality rung.

Pollinations serves keyless Flux images (free, no SLA — the honest
default); Gemini draws through the same AI Studio key the chat provider
and embeddings already use, as the quality option. Either way the prompt
reaches the image provider: that channel is real, and the settings
bound it — prompts are short by design, and a day's images are counted
only when one actually came back.

The made image is a stored file, not an act. It goes nowhere until a
separate share or send moves it, and anything beyond the owner's own
chats waits for approval like every outbound message.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

import httpx
from google import genai
from google.genai import types as gtypes

from ..counters import today
from .store import MediaStoreFull, sniff_media_type

log = logging.getLogger("aether.media")

# the quality rung's model — the image-generation flash, not the
# image-understanding model (llm.image_model reads; this one draws)
_GEMINI_IMAGE_MODEL = "gemini-2.5-flash-image"
# a made image is a few MB; anything past this is not a picture
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_IMAGE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

_SWITCHED_OFF = "image making is switched off in settings (media.image_provider is none)."
_NO_KEY = (
    "image making is set to gemini but no GEMINI_API_KEY is set — "
    "add it in settings, or point media.image_provider at pollinations."
)


def _retry_delay(exc: Exception) -> float | None:
    """The server's own suggested delay out of a 429; None = give up."""
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status != 429:
        return None
    import re

    match = re.search(r"retry in ([\d.]+)s", str(exc), re.IGNORECASE)
    return min(float(match.group(1)) + 1.0, 120.0) if match else 5.0


class ImageGenerator:
    """The generate_image backing: two providers, one capped seam.

    `resolver` hands the gemini rung its key; `counters` enforces the
    daily image cap; `media` stores what comes back so the model can
    share it. `client` is an optional shared httpx client — production
    opens one per call, tests hand in a mock transport so no test ever
    touches the network."""

    def __init__(
        self,
        resolver: Any,
        counters: Any,
        config: Any,
        media: Any,
        client: httpx.AsyncClient | None = None,
        gemini: Any = None,
    ) -> None:
        self._resolver = resolver
        self._counters = counters
        self._config = config
        self._media = media
        self._client = client
        # an injected genai-shaped client — production builds one per
        # call from the resolved key, tests hand in a double
        self._gemini = gemini

    async def generate_tool(self, params: dict) -> str:
        """The generate_image tool handler — a plain refusal at every
        gate, a made image stored when one comes back. The cap counts an
        image only when the provider delivered one; a failed call spends
        nothing."""
        prompt = str(params.get("prompt", "")).strip()
        if not prompt:
            return "generate_image needs a prompt."
        if self._config.media.image_provider == "none":
            return _SWITCHED_OFF
        max_chars = self._config.media.max_prompt_chars
        if len(prompt) > max_chars:
            return (
                f"the prompt is longer than the {max_chars}-character limit — "
                "ask for something shorter."
            )
        cap = self._config.media.max_images_per_day
        day = today()
        if await self._counters.value(day, "images") >= cap:
            return f"the day's image limit ({cap}) is already used — more tomorrow."
        try:
            if self._config.media.image_provider == "gemini":
                key = await self._resolver.resolve("GEMINI_API_KEY")
                if not key:
                    return _NO_KEY
                data = await self._via_gemini(prompt, key)
            else:
                data = await self._via_pollinations(prompt)
        except (httpx.TimeoutException, TimeoutError):
            return "making the image took too long — nothing else is affected."
        except (httpx.HTTPError, ValueError) as exc:
            log.info("image generation failed: %s", exc)
            return "the image couldn't be made just now — nothing else is affected."
        except Exception as exc:  # a provider error is a plain note, never a crash
            log.info("image generation failed: %s", exc)
            return "the image couldn't be made just now — nothing else is affected."
        sniffed = sniff_media_type(data)
        if not sniffed.startswith("image/") or len(data) > _MAX_IMAGE_BYTES:
            # the sniffer decides what came back, never the provider's word
            return "the image service returned something that isn't an image — not kept."
        await self._counters.bump(day, "images")
        try:
            kept = await self._media.put(
                data,
                filename=f"made-image{_IMAGE_EXTENSIONS.get(sniffed, '.png')}",
                origin="generated",
            )
        except MediaStoreFull:
            return "the image came back but media storage is full — nothing was kept."
        return (
            f"made the image and stored it — media_id {kept.media_id}. "
            "Show it to the user with share_media; sending it anywhere beyond "
            "their own chats waits for approval."
        )

    async def _via_pollinations(self, prompt: str) -> bytes:
        """Keyless Flux — the prompt rides the URL, which is exactly the
        documented channel; the caps bound what can travel."""
        url = f"https://image.pollinations.ai/prompt/{quote(prompt, safe='')}"
        client = self._client or httpx.AsyncClient(timeout=90.0)
        try:
            response = await client.get(
                url, params={"width": "1024", "height": "1024", "nologo": "true"}
            )
            response.raise_for_status()
            data = response.content
        finally:
            if self._client is None:
                await client.aclose()
        if not data:
            raise ValueError("empty image response")
        return data

    async def _via_gemini(self, prompt: str, key: str) -> bytes:
        """The quality rung — the same AI Studio key, the image flash.
        Free-tier 429s carry a retry hint; wait it out like the chat
        provider does."""
        client = self._gemini or genai.Client(api_key=key)
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                response = await client.aio.models.generate_content(
                    model=_GEMINI_IMAGE_MODEL,
                    contents=prompt,
                    config=gtypes.GenerateContentConfig(response_modalities=["IMAGE"]),
                )
                for candidate in response.candidates or []:
                    for part in (candidate.content.parts if candidate.content else []) or []:
                        blob = part.inline_data
                        if blob is not None and blob.data:
                            return bytes(blob.data)
                raise ValueError("no image came back")
            except Exception as exc:
                last_exc = exc
                delay = _retry_delay(exc)
                if delay is None or attempt == 2:
                    break
                await asyncio.sleep(delay)
        raise ValueError(f"gemini image generation failed: {last_exc}")
