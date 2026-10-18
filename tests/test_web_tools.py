"""The web tools — the fetch hardening and the search gate, exercised with
mock transports so no test ever touches a network.

What's pinned here: the address refusal list (private, loopback, the cloud
metadata addresses, unique-local, CGNAT, the special-purpose ranges), the
per-hop re-check (a redirect onto an internal address is refused, not
followed), the redirect cap, the byte cap, the scheme and credential
refusals, the key gate on search, the daily cap, and the one rule that
carries the design: results are data, returned as data.
"""

from __future__ import annotations

import ipaddress

import httpx
import pytest

import aether.web as web
from aether.config import AppConfig
from aether.web import WebRefused, fetch_page, tavily_search

PUBLIC = ipaddress.ip_address("93.184.216.34")


# -- fakes --------------------------------------------------------------------


class FakeResolver:
    """The resolver surface WebTools needs — one canned answer per name."""

    def __init__(self, answer: str | None) -> None:
        self.answer = answer
        self.asked: list[str] = []

    async def resolve(self, name: str) -> str | None:
        self.asked.append(name)
        return self.answer


class FakeCounters:
    """The counter surface WebTools needs — in-memory, per (day, field)."""

    def __init__(self, values: dict | None = None) -> None:
        self.values = dict(values or {})
        self.bumps: list[tuple[str, str, int]] = []

    async def value(self, day: str, field: str) -> int:
        return self.values.get((day, field), 0)

    async def bump(self, day: str, field: str, n: int = 1) -> int:
        self.bumps.append((day, field, n))
        key = (day, field)
        self.values[key] = self.values.get(key, 0) + n
        return self.values[key]


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


@pytest.fixture
def public_dns(monkeypatch):
    """Names resolve to one public address; IP literals go through the
    real parse, so the address rules are what's under test, hermetically."""

    def fake_resolve(host: str):
        try:
            return [ipaddress.ip_address(host)]
        except ValueError:
            return [PUBLIC]

    monkeypatch.setattr(web, "_resolve_host", fake_resolve)


def tools(resolver: FakeResolver, counters: FakeCounters, handler=None):
    client = mock_client(handler) if handler else None
    return web.WebTools(
        resolver=resolver,
        counters=counters,
        config=AppConfig(),
        client=client,
    )


# -- the address refusal list ------------------------------------------------


@pytest.mark.parametrize(
    "addr",
    [
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",  # private
        "127.0.0.1",  # loopback
        "169.254.169.254",
        "169.254.0.4",  # link-local — the cloud metadata range
        "100.64.0.1",  # shared CGNAT
        "0.0.0.1",
        "240.0.0.1",  # this-network and reserved
        "192.0.2.1",
        "198.51.100.7",
        "203.0.113.9",  # TEST-NET documentation ranges
        "::1",
        "fd00::1",
        "fe80::1",  # v6 loopback, unique-local, link-local
        "ff02::1",  # multicast
    ],
)
def test_refusal_list_covers_every_internal_range(addr: str) -> None:
    assert web._refused(ipaddress.ip_address(addr))


@pytest.mark.parametrize("addr", ["93.184.216.34", "8.8.8.8", "2606:4700::1111"])
def test_public_addresses_pass(addr: str) -> None:
    assert not web._refused(ipaddress.ip_address(addr))


# -- fetch hardening ----------------------------------------------------------


async def test_scheme_must_be_http_or_https() -> None:
    for url in ("ftp://example.com/x", "file:///etc/passwd", "gopher://example.com"):
        with pytest.raises(WebRefused):
            await fetch_page(url)


async def test_embedded_credentials_are_never_fetched() -> None:
    with pytest.raises(WebRefused):
        await fetch_page("http://user:secret@example.com/")


async def test_literal_internal_addresses_are_refused_before_any_request() -> None:
    for url in (
        "http://10.0.0.5/",
        "http://169.254.169.254/latest/meta-data",
        "http://[fd12::1]/",
        "http://127.0.0.1:8000/",
    ):
        with pytest.raises(WebRefused):
            await fetch_page(url)


async def test_a_host_resolving_internal_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(web, "_resolve_host", lambda host: [ipaddress.ip_address("10.1.2.3")])
    with pytest.raises(WebRefused):
        await fetch_page("http://innocent.example/")


async def test_mixed_public_and_internal_answers_refuse(monkeypatch) -> None:
    # the classic rebinding shape: one public record to pass a naive check,
    # one internal record for the actual connection — any internal answer
    # refuses the whole fetch
    monkeypatch.setattr(
        web,
        "_resolve_host",
        lambda host: [PUBLIC, ipaddress.ip_address("192.168.0.9")],
    )
    with pytest.raises(WebRefused):
        await fetch_page("http://rebind.example/")


async def test_redirects_are_rechecked_per_hop(public_dns) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "start" in request.url.path:
            return httpx.Response(302, headers={"location": "http://169.254.169.254/secret"})
        raise AssertionError("the internal hop must never be requested")

    with pytest.raises(WebRefused):
        await fetch_page("http://example.com/start", client=mock_client(handler))


async def test_redirects_stop_at_the_cap(public_dns) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": str(request.url)})

    with pytest.raises(WebRefused):
        await fetch_page("http://example.com/a", client=mock_client(handler))


async def test_a_page_comes_back_as_plain_text(public_dns) -> None:
    body = (
        "<html><head><title>Enough About Me</title>"
        "<style>body{display:none}</style>"
        "<script>steal()</script></head>"
        "<body><h1>Enough About Me</h1><p>It began with a list.</p></body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})

    page = await fetch_page("http://example.com/me", client=mock_client(handler))
    assert page.title == "Enough About Me"
    assert "It began with a list." in page.text
    assert "steal" not in page.text
    assert "display:none" not in page.text
    assert not page.truncated


async def test_the_body_stops_at_the_byte_cap(public_dns) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"x" * (3 * 1024 * 1024), headers={"content-type": "text/plain"}
        )

    page = await fetch_page("http://example.com/big", client=mock_client(handler))
    assert page.truncated
    assert len(page.text) <= 2 * 1024 * 1024


async def test_binary_content_is_refused_in_plain_words(public_dns) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\x89PNG...", headers={"content-type": "image/png"})

    with pytest.raises(WebRefused):
        await fetch_page("http://example.com/pic", client=mock_client(handler))


async def test_error_statuses_refuse_in_plain_words(public_dns) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="gone")

    with pytest.raises(WebRefused):
        await fetch_page("http://example.com/nope", client=mock_client(handler))


async def test_the_fetch_tool_clips_and_notes_a_long_page(public_dns) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="word " * 20000, headers={"content-type": "text/plain"})

    kit = tools(FakeResolver(None), FakeCounters(), handler)
    out = await kit.fetch_tool({"url": "http://example.com/long"})
    assert out.startswith("(no title) — http://example.com/long")
    assert "longer than what was kept" in out


async def test_the_fetch_tool_needs_a_url() -> None:
    kit = tools(FakeResolver(None), FakeCounters())
    assert "needs a url" in await kit.fetch_tool({})


# -- search --------------------------------------------------------------------


async def test_tavily_search_returns_results_as_data() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.tavily.com"
        assert request.headers["authorization"] == "Bearer tvly-test"
        payload = request.read().decode()
        assert "enamel" in payload
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "Tea", "url": "https://example.com/tea", "content": "Stains enamel."},
                ]
            },
        )

    results = await tavily_search(
        "does tea stain enamel", api_key="tvly-test", client=mock_client(handler)
    )
    assert results == [
        {"title": "Tea", "url": "https://example.com/tea", "snippet": "Stains enamel."}
    ]


async def test_a_refused_search_key_says_so_plainly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    with pytest.raises(WebRefused):
        await tavily_search("anything", api_key="bad", client=mock_client(handler))


async def test_search_without_a_key_is_inert() -> None:
    counters = FakeCounters()
    kit = tools(FakeResolver(None), counters)
    out = await kit.search_tool({"query": "anything"})
    assert "isn't connected" in out
    assert counters.bumps == []  # an inert search spends no cap


async def test_search_runs_only_under_the_daily_cap() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    counters = FakeCounters({(web.today(), "searches"): 25})
    kit = tools(FakeResolver("tvly-test"), counters, handler)
    out = await kit.search_tool({"query": "one more"})
    assert "daily search cap" in out
    assert counters.bumps == []


async def test_a_successful_search_spends_one_unit_of_cap() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "T", "url": "https://e.com/t", "content": "c"},
                ]
            },
        )

    counters = FakeCounters()
    kit = tools(FakeResolver("tvly-test"), counters, handler)
    out = await kit.search_tool({"query": "tea", "max_results": "not a number"})
    assert "T — https://e.com/t" in out
    assert counters.bumps == [(web.today(), "searches", 1)]


async def test_search_needs_a_query() -> None:
    kit = tools(FakeResolver("tvly-test"), FakeCounters())
    assert "needs a query" in await kit.search_tool({})
