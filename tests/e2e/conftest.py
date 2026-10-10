"""Boots the real Aether web app (dev_server.build_app) on a free local
port for each test, against fresh in-memory fakes — no Postgres, no LLM
key. `pytest-playwright` supplies the `page` fixture this pairs with.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn

from .dev_server import API_TOKEN, build_app

WEB_DIR = Path(__file__).resolve().parents[2] / "src" / "aether" / "web"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _ServerThread(threading.Thread):
    def __init__(self, port: int) -> None:
        super().__init__(daemon=True)
        config = uvicorn.Config(build_app(), host="127.0.0.1", port=port, log_level="warning")
        self.server = uvicorn.Server(config)

    def run(self) -> None:
        self.server.run()

    def stop(self) -> None:
        self.server.should_exit = True


@pytest.fixture
def live_server() -> Iterator[str]:
    """Yields the base URL of a freshly booted dev-fixture server."""
    if not (WEB_DIR / "index.html").is_file():
        pytest.skip(
            "panel not built — run: cd webapp && npm install && npm run build",
        )
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    thread = _ServerThread(port)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{base_url}/", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        raise RuntimeError("dev fixture server did not come up in time")
    yield base_url
    thread.stop()
    thread.join(timeout=5)


@pytest.fixture
def token() -> str:
    return API_TOKEN
