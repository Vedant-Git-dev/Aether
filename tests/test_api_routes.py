"""API route tests — screen-capture upload against a stub app.state."""

from __future__ import annotations

import httpx
from fastapi import FastAPI

from aether.api.routes import router
from aether.config import Settings
from aether.memory.events import IngestResult


class FakeScreenVision:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, str]] = []

    async def record(self, png: bytes, note: str = ""):
        self.calls.append((png, note))
        return IngestResult(stored=True, reason="new", event_id=1)


def _app(vision) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.settings = Settings(_env_file=None, api_token="secret")
    if vision is not None:
        app.state.screen_vision = vision
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_screen_capture_rejects_a_bad_token() -> None:
    vision = FakeScreenVision()
    async with _client(_app(vision)) as client:
        response = await client.post(
            "/api/screen-capture",
            params={"token": "wrong"},
            files={"file": ("s.png", b"png", "image/png")},
            data={"note": ""},
        )
    assert response.status_code == 401
    assert vision.calls == []


async def test_screen_capture_accepts_the_upload() -> None:
    vision = FakeScreenVision()
    async with _client(_app(vision)) as client:
        response = await client.post(
            "/api/screen-capture",
            params={"token": "secret"},
            files={"file": ("s.png", b"png-bytes", "image/png")},
            data={"note": "keep an eye on this"},
        )
    assert response.status_code == 202
    assert response.json() == {"stored": True, "reason": "new", "event_id": 1}
    assert vision.calls == [(b"png-bytes", "keep an eye on this")]


async def test_screen_capture_token_via_header() -> None:
    vision = FakeScreenVision()
    async with _client(_app(vision)) as client:
        response = await client.post(
            "/api/screen-capture",
            headers={"x-aether-token": "secret"},
            files={"file": ("s.png", b"png", "image/png")},
        )
    assert response.status_code == 202


async def test_screen_capture_without_vision_configured_is_503() -> None:
    async with _client(_app(None)) as client:  # no screen_vision on state
        response = await client.post(
            "/api/screen-capture",
            params={"token": "secret"},
            files={"file": ("s.png", b"png", "image/png")},
        )
    assert response.status_code == 503


async def test_screen_capture_rejects_an_empty_upload() -> None:
    vision = FakeScreenVision()
    async with _client(_app(vision)) as client:
        response = await client.post(
            "/api/screen-capture",
            params={"token": "secret"},
            files={"file": ("s.png", b"", "image/png")},
        )
    assert response.status_code == 400
