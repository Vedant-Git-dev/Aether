"""The image generation seam — two providers behind one capped tool,
exercised with mock transports and genai doubles so no test ever makes
a real image or touches a network.

What's pinned here: the provider switch (pollinations default, gemini
quality rung, none inert), the prompt length cap, the daily counter gate
and its bump-only-on-success rule, the sniffer's say over what came
back, and the stored-not-sent framing of the result note.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from urllib.parse import quote

import httpx
from fakes import FakeMediaStore

from aether.config import AppConfig, MediaConfig
from aether.counters import today
from aether.media.generate import ImageGenerator

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
_HTML = b"<html><body>not a picture</body></html>"
_SUCCESS_NOTE = (
    "made the image and stored it — media_id 1. Show it to the user with "
    "share_media; sending it anywhere beyond their own chats waits for approval."
)


# -- fakes --------------------------------------------------------------------


class FakeResolver:
    """The resolver seam — one canned answer, every ask recorded."""

    def __init__(self, answer: str | None) -> None:
        self.answer = answer
        self.asked: list[str] = []

    async def resolve(self, name: str) -> str | None:
        self.asked.append(name)
        return self.answer


class FakeCounters:
    """The counter seam — one int per (day, field), every bump recorded."""

    def __init__(self, values: dict | None = None) -> None:
        self.values = dict(values or {})
        self.bumps: list[tuple[str, str, int]] = []

    async def value(self, day: str, field: str) -> int:
        return self.values.get((day, field), 0)

    async def bump(self, day: str, field: str, n: int = 1) -> int:
        self.bumps.append((day, field, n))
        self.values[(day, field)] = self.values.get((day, field), 0) + n
        return self.values[(day, field)]


class FakeGemini:
    """A genai-shaped client double — aio.models.generate_content with a
    canned response, an optional scripted failure (raised for the first
    `failures` calls, so a retry loop can be exercised), every ask
    recorded."""

    def __init__(
        self, data: bytes | None = None, exc: Exception | None = None, failures: int = 0
    ) -> None:
        self.calls: list[dict] = []
        self._data = data
        self._exc = exc
        self._failures = failures
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._generate))

    async def _generate(self, model: str, contents, config=None) -> SimpleNamespace:
        self.calls.append({"model": model, "contents": contents})
        if self._exc is not None and len(self.calls) <= self._failures:
            raise self._exc
        parts = []
        if self._data is not None:
            parts.append(SimpleNamespace(inline_data=SimpleNamespace(data=self._data)))
        return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))])


def generator(
    resolver: FakeResolver | None = None,
    counters: FakeCounters | None = None,
    config: AppConfig | None = None,
    media: FakeMediaStore | None = None,
    handler=None,
    gemini: FakeGemini | None = None,
) -> ImageGenerator:
    return ImageGenerator(
        resolver if resolver is not None else FakeResolver("a-key"),
        counters if counters is not None else FakeCounters(),
        config if config is not None else AppConfig(),
        media if media is not None else FakeMediaStore(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)) if handler else None,
        gemini=gemini,
    )


# -- the gates -----------------------------------------------------------------


async def test_generate_image_needs_a_prompt() -> None:
    kit = generator()
    assert await kit.generate_tool({}) == "generate_image needs a prompt."
    assert await kit.generate_tool({"prompt": "   "}) == "generate_image needs a prompt."


async def test_a_switched_off_provider_makes_nothing() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_PNG)

    kit = generator(config=AppConfig(media=MediaConfig(image_provider="none")), handler=handler)
    assert await kit.generate_tool({"prompt": "a cat"}) == (
        "image making is switched off in settings (media.image_provider is none)."
    )
    assert seen == []


async def test_a_prompt_past_the_length_cap_is_refused_before_any_call() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_PNG)

    kit = generator(config=AppConfig(media=MediaConfig(max_prompt_chars=10)), handler=handler)
    assert await kit.generate_tool({"prompt": "a very long prompt indeed"}) == (
        "the prompt is longer than the 10-character limit — ask for something shorter."
    )
    assert seen == []


async def test_the_day_s_limit_spends_nothing() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_PNG)

    counters = FakeCounters({(today(), "images"): 10})
    store = FakeMediaStore()
    kit = generator(counters=counters, media=store, handler=handler)

    assert await kit.generate_tool({"prompt": "a cat"}) == (
        "the day's image limit (10) is already used — more tomorrow."
    )
    assert seen == []
    assert counters.bumps == []
    assert store.rows == {}


# -- pollinations, the keyless default ------------------------------------------


async def test_pollinations_stores_the_image_and_names_its_media_id() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_PNG)

    resolver = FakeResolver("a-key")  # the default rung never asks for a key
    store = FakeMediaStore()
    counters = FakeCounters()
    kit = generator(resolver=resolver, counters=counters, media=store, handler=handler)

    assert await kit.generate_tool({"prompt": "a red cat"}) == _SUCCESS_NOTE

    # the prompt rides the URL — the one documented channel, quoted whole
    assert quote("a red cat", safe="") in str(seen[0].url)
    assert resolver.asked == []
    # the sniffer named what came back, and the file is stored under it
    assert store.rows[1].media_type == "image/png"
    assert store.rows[1].filename == "made-image.png"
    # and the day's cap spent exactly one image
    assert counters.bumps == [(today(), "images", 1)]


async def test_a_failed_call_spends_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    store = FakeMediaStore()
    counters = FakeCounters()
    kit = generator(counters=counters, media=store, handler=handler)

    assert await kit.generate_tool({"prompt": "a cat"}) == (
        "the image couldn't be made just now — nothing else is affected."
    )
    assert counters.bumps == []
    assert store.rows == {}


async def test_a_slow_provider_is_a_plain_note_not_a_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    store = FakeMediaStore()
    counters = FakeCounters()
    kit = generator(counters=counters, media=store, handler=handler)

    assert await kit.generate_tool({"prompt": "a cat"}) == (
        "making the image took too long — nothing else is affected."
    )
    assert counters.bumps == []
    assert store.rows == {}


async def test_the_sniffer_decides_what_came_back() -> None:
    """A provider that answers with HTML never reaches the store — the
    magic bytes say what it is, not the response's word."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_HTML)

    store = FakeMediaStore()
    counters = FakeCounters()
    kit = generator(counters=counters, media=store, handler=handler)

    assert await kit.generate_tool({"prompt": "a cat"}) == (
        "the image service returned something that isn't an image — not kept."
    )
    assert counters.bumps == []
    assert store.rows == {}


async def test_an_overlarge_image_is_not_kept() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_PNG + b"\x00" * (10 * 1024 * 1024))

    store = FakeMediaStore()
    counters = FakeCounters()
    kit = generator(counters=counters, media=store, handler=handler)

    assert await kit.generate_tool({"prompt": "a cat"}) == (
        "the image service returned something that isn't an image — not kept."
    )
    assert counters.bumps == []
    assert store.rows == {}


async def test_a_full_store_keeps_the_image_nowhere_but_says_so() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_PNG)

    store = FakeMediaStore()
    store.full = True
    counters = FakeCounters()
    kit = generator(counters=counters, media=store, handler=handler)

    assert await kit.generate_tool({"prompt": "a cat"}) == (
        "the image came back but media storage is full — nothing was kept."
    )
    assert store.rows == {}
    # the provider did make the image — the day's cap counts it anyway
    assert counters.bumps == [(today(), "images", 1)]


# -- gemini, the keyed quality rung ---------------------------------------------


async def test_gemini_without_a_key_says_so_in_plain_words() -> None:
    gemini = FakeGemini(data=_PNG)
    resolver = FakeResolver(None)
    kit = generator(
        resolver=resolver,
        config=AppConfig(media=MediaConfig(image_provider="gemini")),
        gemini=gemini,
    )

    assert await kit.generate_tool({"prompt": "a cat"}) == (
        "image making is set to gemini but no GEMINI_API_KEY is set — "
        "add it in settings, or point media.image_provider at pollinations."
    )
    assert resolver.asked == ["GEMINI_API_KEY"]
    assert gemini.calls == []


async def test_gemini_draws_through_the_keyed_rung() -> None:
    gemini = FakeGemini(data=_PNG)
    resolver = FakeResolver("a-key")
    store = FakeMediaStore()
    counters = FakeCounters()
    kit = generator(
        resolver=resolver,
        counters=counters,
        media=store,
        config=AppConfig(media=MediaConfig(image_provider="gemini")),
        gemini=gemini,
    )

    assert await kit.generate_tool({"prompt": "a red cat"}) == _SUCCESS_NOTE

    assert gemini.calls == [{"model": "gemini-2.5-flash-image", "contents": "a red cat"}]
    assert store.rows[1].media_type == "image/png"
    assert counters.bumps == [(today(), "images", 1)]


async def test_a_gemini_throttle_is_waited_out_like_the_chat_provider(monkeypatch) -> None:
    """The 429 carries the server's own retry hint — the second attempt
    wins, and the wait is the parsed hint plus the safety second."""

    class Throttled(Exception):
        status_code = 429

        def __init__(self) -> None:
            super().__init__("quota exceeded — retry in 0.5s")

    slept: list[float] = []

    async def no_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr("aether.media.generate.asyncio.sleep", no_sleep)
    gemini = FakeGemini(data=_PNG, exc=Throttled(), failures=1)
    store = FakeMediaStore()
    counters = FakeCounters()
    kit = generator(
        counters=counters,
        media=store,
        config=AppConfig(media=MediaConfig(image_provider="gemini")),
        gemini=gemini,
    )

    assert await kit.generate_tool({"prompt": "a red cat"}) == _SUCCESS_NOTE

    assert len(gemini.calls) == 2  # refused once, retried, delivered
    assert slept == [1.5]  # the server's 0.5s hint plus the safety second
    assert counters.bumps == [(today(), "images", 1)]


async def test_a_gemini_failure_beyond_the_throttle_is_a_plain_note() -> None:
    gemini = FakeGemini(exc=ValueError("the model is down"))
    store = FakeMediaStore()
    counters = FakeCounters()
    kit = generator(
        counters=counters,
        media=store,
        config=AppConfig(media=MediaConfig(image_provider="gemini")),
        gemini=gemini,
    )

    assert await kit.generate_tool({"prompt": "a cat"}) == (
        "the image couldn't be made just now — nothing else is affected."
    )
    assert len(gemini.calls) == 1  # not a throttle — no retry
    assert counters.bumps == []
    assert store.rows == {}
