"""Voice transcription tests — GroqTranscriber against a mocked Groq REST
(httpx MockTransport); no network, no DB."""

from __future__ import annotations

import httpx

from aether.media.voice import GroqTranscriber


class FakeResolver:
    """The EnvResolver double: secrets by name, on demand."""

    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    async def resolve(self, name: str) -> str | None:
        return self.values.get(name)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_a_voice_note_is_transcribed_through_the_groq_rest() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"text": "the porch light is still on"})

    transcriber = GroqTranscriber(FakeResolver({"GROQ_API_KEY": "gsk-test"}), _client(handler))
    text = await transcriber.transcribe(b"ogg-bytes", "voice.ogg", "audio/ogg")
    assert text == "the porch light is still on"

    (request,) = seen
    assert request.url == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert request.headers["Authorization"] == "Bearer gsk-test"
    body = request.read()
    assert b"whisper-large-v3" in body  # the model rode along
    assert b"voice.ogg" in body  # and the file, by name


async def test_without_a_key_nothing_is_sent_and_none_comes_back() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should leave without a key")

    transcriber = GroqTranscriber(FakeResolver({}), _client(handler))
    assert await transcriber.transcribe(b"ogg-bytes", "voice.ogg") is None


async def test_a_failed_or_empty_answer_comes_back_as_none() -> None:
    broken = GroqTranscriber(
        FakeResolver({"GROQ_API_KEY": "gsk-test"}),
        _client(lambda request: httpx.Response(500, json={"error": "overloaded"})),
    )
    assert await broken.transcribe(b"ogg-bytes", "voice.ogg") is None

    empty = GroqTranscriber(
        FakeResolver({"GROQ_API_KEY": "gsk-test"}),
        _client(lambda request: httpx.Response(200, json={"text": "   "})),
    )
    assert await empty.transcribe(b"ogg-bytes", "voice.ogg") is None
