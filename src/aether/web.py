"""Hardened web access — read one page, search the web.

Aether's only reach onto the public web. Two helpers, both read-only:

- fetch_page: an SSRF-hardened GET. http/https only; the hostname is
  resolved and every address it yields is checked against a refusal
  list (private, loopback, link-local — which covers the cloud metadata
  addresses at 169.254.x.x — unique-local, shared CGNAT, reserved and
  documentation ranges), and the check runs again on every redirect hop.
  Redirects are followed by hand (cap: 2) so no hop can slip past the
  address rules inside a library's redirect loop. The body streams and
  aborts past 2 MB; HTML is stripped to plain text.

- tavily_search: the web_search backing, against the search REST API.
  Key-gated (TAVILY_API_KEY via the resolver) and inert without it.

The one rule that matters most, in code and in the README: fetched pages
and search results are DATA. They can say anything — including things
addressed to Aether — and nothing they say is ever treated as an
instruction. The only path from web content into anything the agent keeps
is the audited workspace tool, after the user confirms it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from .counters import today

if TYPE_CHECKING:
    from .config import AppConfig


class KeyResolver(Protocol):
    """The resolver surface the web tools need — one name in, one key out."""

    async def resolve(self, name: str) -> str | None: ...


class DayCounters(Protocol):
    """The counter surface the web tools need — a day key and a field."""

    async def value(self, day: str, field: str) -> int: ...

    async def bump(self, day: str, field: str, n: int = 1) -> int: ...


log = logging.getLogger("aether.web")

_SCHEMES = ("http", "https")
_MAX_REDIRECTS = 2
_MAX_BYTES = 2 * 1024 * 1024
_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 20.0
_TOTAL_TIMEOUT = 30.0
_DNS_TIMEOUT = 10.0
_USER_AGENT = "Aether/0.1 (a personal assistant)"
_SEARCH_URL = "https://api.tavily.com/search"
# how much of a page the tool hands the model — the fetch itself keeps
# everything it read up to the byte cap, the tool surface stays small
_TOOL_TEXT_LIMIT = 6000
_SNIPPET_LIMIT = 400
_MAX_RESULTS = 10

NOT_CONNECTED = (
    "web search isn't connected right now — no search key is set. "
    "One can be pasted on the settings page."
)
_CAP_REACHED = "the daily search cap is reached ({cap} a day) — try again tomorrow."


class WebRefused(ValueError):
    """A fetch or search the hardening refuses — plain words, never a crash."""


# The refusal list, as an allowlist: only globally-routable unicast
# addresses pass. is_global already refuses RFC1918, loopback, link-local
# (including every cloud metadata address), unique-local, CGNAT and
# reserved space; the explicit ranges below are defense in depth for the
# special-purpose blocks whose is_global has exceptions (TEST-NET and
# friends), so the two can never drift apart.
_EXTRA_REFUSED_V4 = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "100.64.0.0/10",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "240.0.0.0/4",
    )
)


def _refused(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if not addr.is_global or addr.is_multicast or addr.is_unspecified:
        return True
    return isinstance(addr, ipaddress.IPv4Address) and any(addr in net for net in _EXTRA_REFUSED_V4)


def _resolve_host(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Every address a hostname yields. A literal IP is checked directly; a
    name is resolved, and ANY refused address in the answer refuses the
    whole fetch — a DNS record set with one public and one internal
    address is the classic rebinding trick, and it fails closed."""
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        pass
    else:
        return [literal]
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]) for info in infos]


async def _check_target(url: str) -> None:
    """One hop's URL against the rules — scheme, host, credentials, and
    every address the host resolves to. Runs before the request on every
    hop, redirects included."""
    parts = urlsplit(url)
    if parts.scheme not in _SCHEMES:
        raise WebRefused(
            f"only http and https pages can be fetched, not {parts.scheme or 'a bare path'}."
        )
    host = parts.hostname
    if not host:
        raise WebRefused("that address has no host to fetch from.")
    if parts.username or parts.password:
        raise WebRefused("an address carrying embedded credentials is never fetched.")
    try:
        infos = await asyncio.wait_for(asyncio.to_thread(_resolve_host, host), timeout=_DNS_TIMEOUT)
    except (socket.gaierror, TimeoutError, OSError, ValueError) as exc:
        raise WebRefused(f"couldn't resolve {host} — not fetched.") from exc
    if not infos:
        raise WebRefused(f"{host} resolved to no address — not fetched.")
    if any(_refused(addr) for addr in infos):
        raise WebRefused(f"{host} points at an address I won't fetch from — refused.")


@dataclass(frozen=True)
class FetchedPage:
    """One page as the agent reads it — plain text, never raw HTML."""

    title: str
    url: str  # the final address, after any redirects
    text: str
    truncated: bool  # the byte cap stopped the read early
    content_type: str


class _TextExtractor(HTMLParser):
    """HTML to readable text: script/style/template content dropped, block
    boundaries become newlines, the title is kept separately."""

    _SKIP = frozenset({"script", "style", "noscript", "template", "svg"})
    _BLOCKS = frozenset(
        {
            "p",
            "br",
            "div",
            "li",
            "tr",
            "table",
            "section",
            "article",
            "header",
            "footer",
            "blockquote",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._title: list[str] = []
        self._in_title = False
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self._BLOCKS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        (self._title if self._in_title else self._chunks).append(data)

    @property
    def title(self) -> str:
        return " ".join("".join(self._title).split())

    @property
    def text(self) -> str:
        raw = "".join(self._chunks)
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        out: list[str] = []
        for line in lines:
            if line or (out and out[-1]):
                out.append(line)
        return "\n".join(out).strip()


def _page_text(body: bytes, content_type: str, charset: str | None) -> tuple[str, str]:
    """(title, text) from one response body — HTML stripped to text, other
    text-like content passed through, anything binary refused in plain
    words."""
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    decoded = body.decode(charset or "utf-8", errors="replace")
    if ct in ("text/html", "application/xhtml+xml") or ct.endswith(("+html",)):
        extractor = _TextExtractor()
        try:
            extractor.feed(decoded)
            extractor.close()
        except Exception:  # malformed HTML still yields whatever parsed
            log.debug("html extraction was incomplete", exc_info=True)
        return extractor.title, extractor.text
    if (
        ct.startswith("text/")
        or ct in ("application/json",)
        or ct.endswith(("+json", "+xml", "xml"))
    ):
        return "", decoded.strip()
    raise WebRefused(f"that page isn't text ({ct or 'unknown type'}) — not fetched.")


async def fetch_page(url: str, *, client: httpx.AsyncClient | None = None) -> FetchedPage:
    """Fetch one page, hardened: address rules re-checked per hop, redirects
    followed by hand up to 2, the body streamed and aborted past 2 MB.

    The client, when handed in, must not follow redirects itself — the
    hops are validated here, one by one. When it isn't handed in, a
    client with the house timeouts is created and closed per call.
    """
    owned = client is None
    if client is None:
        client = httpx.AsyncClient(
            follow_redirects=False,
            headers={"user-agent": _USER_AGENT},
            timeout=httpx.Timeout(_TOTAL_TIMEOUT, connect=_CONNECT_TIMEOUT, read=_READ_TIMEOUT),
        )
    current = url
    redirects_left = _MAX_REDIRECTS
    try:
        while True:
            await _check_target(current)
            async with client.stream("GET", current) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        raise WebRefused(
                            f"the page answered with a redirect to nowhere ({resp.status_code})."
                        )
                    if redirects_left <= 0:
                        raise WebRefused("stopped — that page redirects too many times.")
                    redirects_left -= 1
                    current = urljoin(current, location)
                    continue
                if resp.status_code >= 400:
                    raise WebRefused(f"the page answered with an error ({resp.status_code}).")
                chunks: list[bytes] = []
                total = 0
                truncated = False
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_BYTES:
                        truncated = True
                        break
                    chunks.append(chunk)
                content_type = resp.headers.get("content-type", "")
                charset = None
                if "charset=" in content_type.lower():
                    charset = (
                        content_type.lower()
                        .split("charset=", 1)[1]
                        .split(";", 1)[0]
                        .strip()
                        .strip("'\"")
                        or None
                    )
                title, text = _page_text(b"".join(chunks), content_type, charset)
                return FetchedPage(
                    title=title,
                    url=str(resp.url),
                    text=text,
                    truncated=truncated,
                    content_type=content_type,
                )
    finally:
        if owned:
            await client.aclose()


async def tavily_search(
    query: str,
    *,
    api_key: str,
    max_results: int = 5,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, str]]:
    """One web search — titles, addresses and snippets, as data. The key is
    the caller's to resolve; an empty result is a fine answer, a refused
    key is a plain-words error."""
    owned = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(_TOTAL_TIMEOUT, connect=_CONNECT_TIMEOUT))
    try:
        resp = await client.post(
            _SEARCH_URL,
            json={"query": query, "max_results": max(1, min(_MAX_RESULTS, max_results))},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        if resp.status_code in (401, 403):
            raise WebRefused("the search key wasn't accepted — web search is off until it's fixed.")
        if resp.status_code >= 400:
            raise WebRefused(f"the search service answered with an error ({resp.status_code}).")
        data = resp.json()
    finally:
        if owned:
            await client.aclose()
    return [
        {
            "title": str(r.get("title") or ""),
            "url": str(r.get("url") or ""),
            "snippet": str(r.get("content") or ""),
        }
        for r in data.get("results") or []
        if isinstance(r, dict)
    ]


def build_client() -> httpx.AsyncClient:
    """One long-lived client with the house timeouts and user-agent, for
    main.py to build once and hand to WebTools — every web_fetch/web_search
    call then reuses its connection pool instead of paying a fresh TCP+TLS
    handshake per call. Redirects stay off: fetch_page follows them by
    hand, hop by hand, so the address rules can't be skipped."""
    return httpx.AsyncClient(
        follow_redirects=False,
        headers={"user-agent": _USER_AGENT},
        timeout=httpx.Timeout(_TOTAL_TIMEOUT, connect=_CONNECT_TIMEOUT, read=_READ_TIMEOUT),
    )


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _int_param(params: dict, key: str, default: int, maximum: int) -> int:
    try:
        value = int(params.get(key, default))
    except (TypeError, ValueError):
        return default
    return max(1, min(maximum, value))


class WebTools:
    """The two web tools the agent gets, wired to the resolver (the key),
    the daily counters (the cap), and the live config. Reads config on
    every call, so a chat-made cap change bites on the next search."""

    def __init__(
        self,
        *,
        resolver: KeyResolver,
        counters: DayCounters,
        config: AppConfig,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._resolver = resolver
        self._counters = counters
        self._config = config
        # main.py hands in one long-lived client (build_client()) so every
        # call reuses its connection pool; tests hand in a mock transport so
        # no test ever touches the network; None still works per call-site
        # default in fetch_page/tavily_search for anything constructed bare
        self._client = client

    async def _search_key(self) -> str | None:
        return await self._resolver.resolve("TAVILY_API_KEY")

    async def fetch_tool(self, params: dict) -> str:
        """The web_fetch tool handler — one page, plain text, capped."""
        url = str(params.get("url", "")).strip()
        if not url:
            return "web_fetch needs a url."
        if "://" not in url:
            url = f"https://{url}"
        try:
            page = await fetch_page(url, client=self._client)
        except WebRefused as exc:
            return f"not fetched: {exc}"
        except (httpx.TimeoutException, TimeoutError):
            return "the page took too long to answer — not fetched."
        except httpx.HTTPError:
            return "the page couldn't be reached — not fetched."
        text = _clip(page.text, _TOOL_TEXT_LIMIT)
        header = f"{page.title or '(no title)'} — {page.url}"
        note = ""
        if page.truncated or len(page.text) > _TOOL_TEXT_LIMIT:
            note = "\n\n(the page was longer than what was kept here)"
        body = f"{header}\n\n{text}" if text else header
        return f"{body}{note}"

    async def search_tool(self, params: dict) -> str:
        """The web_search tool handler — key-gated, capped per day. The cap
        counts a search only when it ran; a failed call spends nothing."""
        query = str(params.get("query", "")).strip()
        if not query:
            return "web_search needs a query."
        max_results = _int_param(params, "max_results", 5, _MAX_RESULTS)
        key = await self._search_key()
        if not key:
            return NOT_CONNECTED
        cap = self._config.agent.max_searches_per_day
        day = today()
        if await self._counters.value(day, "searches") >= cap:
            return _CAP_REACHED.format(cap=cap)
        try:
            results = await tavily_search(
                query, api_key=key, max_results=max_results, client=self._client
            )
        except WebRefused as exc:
            return f"not searched: {exc}"
        except (httpx.TimeoutException, TimeoutError):
            return "the search took too long — not run."
        except httpx.HTTPError:
            return "the search couldn't be run just now — nothing else is affected."
        await self._counters.bump(day, "searches")
        if not results:
            return f"No results for {query!r}."
        return "\n\n".join(
            f"{r['title'] or '(no title)'} — {r['url']}\n{_clip(r['snippet'], _SNIPPET_LIMIT)}"
            for r in results
        )
