"""MCP host tests — a real mcp SDK stub server over stdio, no network, no DB.

The stub (tests/stubs/echo_mcp_server.py) is spawned as a subprocess with
the same interpreter, so these exercise the full client session: spawn,
initialize, list_tools, call_tool, and reconnect-after-failure.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

import pytest

from aether.config import MCPServerConfig, PollTool, TransportConfig
from aether.connectors import mcp_host
from aether.connectors.base import ConnectorUnavailableError, UnknownToolError
from aether.connectors.mcp_host import MCPHost, MCPServerConnection
from aether.secret_env import EnvResolver, SecretEnvError

STUB = Path(__file__).parent / "stubs" / "echo_mcp_server.py"


def _stub_config(name: str = "stub", **kwargs) -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        transport=TransportConfig(type="stdio", command=sys.executable, args=[str(STUB)]),
        **kwargs,
    )


async def test_stub_server_lists_tools_and_calls_them() -> None:
    host = MCPHost([_stub_config()])
    host.start()
    try:
        assert await host.wait_all_ready(timeout=30) == {"stub": True}

        specs = {s.name: s for s in host.tool_specs()}
        assert set(specs) == {"stub__echo", "stub__add", "stub__env", "stub__grow"}
        assert specs["stub__echo"].source == "stub"
        assert "text" in specs["stub__echo"].input_schema.get("properties", {})

        assert await host.call("stub__echo", {"text": "hello world"}) == "hello world"
        assert await host.call("stub__add", {"a": 2, "b": 3}) == "5"
    finally:
        await host.stop()


async def test_tool_level_errors_come_back_as_text() -> None:
    host = MCPHost([_stub_config()])
    host.start()
    try:
        await host.wait_all_ready(timeout=30)
        # the server reports a bad tool as an error-marked result — that is
        # an answer for the agent to read, not a connection problem, so the
        # session must survive it untouched
        result = await host.call("stub__no_such_tool", {})
        assert result.startswith("error:")
        assert await host.call("stub__echo", {"text": "still here"}) == "still here"
    finally:
        await host.stop()


async def test_transport_failure_breaks_the_hold_and_reconnects() -> None:
    host = MCPHost([_stub_config()])
    host.start()
    try:
        await host.wait_all_ready(timeout=30)
        conn = host.connections[0]
        real_session = conn._session

        class ExplodingSession:  # simulates the transport dying under us
            async def call_tool(self, tool, args):
                raise RuntimeError("transport is dead")

        conn._session = ExplodingSession()
        with pytest.raises(ConnectorUnavailableError):
            await host.call("stub__echo", {"text": "x"})
        # the failure broke the hold; the runner tears the session down and
        # reconnects with backoff. Waiting on the session object (not the
        # ready flag — it is still set until the teardown finishes) makes
        # this deterministic.
        fresh = None
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            session = conn._session
            if session is not None and not isinstance(session, ExplodingSession):
                fresh = session
                break
            await asyncio.sleep(0.1)
        assert fresh is not None, "the connection never came back after the transport failure"
        assert fresh is not real_session
        assert await host.call("stub__echo", {"text": "back again"}) == "back again"
    finally:
        await host.stop()


async def test_disabled_servers_never_spawn() -> None:
    host = MCPHost([_stub_config(enabled=False)])
    host.start()
    assert host.connections == []
    assert await host.wait_all_ready(timeout=1) == {}
    assert host.tool_specs() == []
    await host.stop()


async def test_dead_server_is_reported_not_raised() -> None:
    host = MCPHost(
        [
            MCPServerConfig(
                name="broken",
                transport=TransportConfig(
                    type="stdio", command="/nonexistent/aether-binary", args=[]
                ),
            )
        ]
    )
    host.start()
    try:
        assert await host.wait_all_ready(timeout=2) == {"broken": False}
    finally:
        await host.stop()


async def test_call_routing_errors() -> None:
    host = MCPHost([_stub_config()])
    with pytest.raises(UnknownToolError):
        await host.call("nosuch__tool", {})  # no server by that name
    with pytest.raises(UnknownToolError):
        await host.call("no_separator", {})  # not <server>__<tool>


async def test_apply_servers_diffs_the_live_set() -> None:
    """The chat-made mcp_servers change, applied live: an untouched server
    keeps its session (a reconnect costs a cold start), a removed one stops,
    an added one starts, and a changed one restarts."""
    host = MCPHost([_stub_config()])
    host.start()
    try:
        assert await host.wait_all_ready(timeout=30) == {"stub": True}
        kept = host.connections[0]
        assert await host.call("stub__echo", {"text": "warm"}) == "warm"

        # add a second server; the first is untouched, so its session survives
        out = await host.apply_servers([_stub_config(), _stub_config(name="stub2")])
        assert [c.name for c in out["started"]] == ["stub2"]
        assert out["stopped"] == []
        assert [c.name for c in host.connections] == ["stub", "stub2"]
        assert host.connections[0] is kept
        assert await host.call("stub__echo", {"text": "still warm"}) == "still warm"
        assert await host.call("stub2__echo", {"text": "fresh"}) == "fresh"

        # remove it again — the name is gone from the routing table
        out = await host.apply_servers([_stub_config()])
        assert out["started"] == []
        assert out["stopped"] == ["stub2"]
        assert [c.name for c in host.connections] == ["stub"]
        with pytest.raises(UnknownToolError):
            await host.call("stub2__echo", {"text": "gone"})
    finally:
        await host.stop()


async def test_apply_servers_restarts_a_changed_server_and_skips_disabled() -> None:
    host = MCPHost([_stub_config()])
    host.start()
    try:
        assert await host.wait_all_ready(timeout=30) == {"stub": True}
        first = host.connections[0]

        # a structurally changed config (a poll tool added — same server,
        # different settings) means a restart: fresh connection, same name
        changed = _stub_config(poll_tools=[PollTool(tool="echo", every_minutes=10.0)])
        disabled = _stub_config(name="off", enabled=False)
        out = await host.apply_servers([changed, disabled])
        assert out["stopped"] == ["stub"]  # the old session was torn down
        assert [c.name for c in out["started"]] == ["stub"]  # and replaced
        assert [c.name for c in host.connections] == ["stub"]  # disabled never spawns
        assert host.connections[0] is not first
        assert await host.call("stub__echo", {"text": "reloaded"}) == "reloaded"
    finally:
        await host.stop()


# -- $NAME expansion: keys pass to apps by name, resolved at open time ----------


def _env_stub_config(marker_name: str, name: str = "stub") -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        transport=TransportConfig(
            type="stdio",
            command=sys.executable,
            args=[str(STUB)],
            env={marker_name: f"${marker_name}"},
        ),
    )


async def test_env_refs_resolve_and_reach_the_child_process(tmp_path) -> None:
    """A $NAME in transport.env is resolved through .env at open time and
    lands in the child's environment — the fix for the SDK's safe-whitelist
    behavior, which otherwise never passes a parent key down."""
    env_file = tmp_path / ".env"
    env_file.write_text("EXPANSION_TEST_KEY=hush-hush\n")
    host = MCPHost([_env_stub_config("EXPANSION_TEST_KEY")], resolver=EnvResolver(env_file))
    host.start()
    try:
        assert await host.wait_all_ready(timeout=30) == {"stub": True}
        assert await host.call("stub__env", {"name": "EXPANSION_TEST_KEY"}) == "hush-hush"
        # the safe defaults still ride along underneath (the child can run)
        assert await host.call("stub__env", {"name": "PATH"}) != ""
    finally:
        await host.stop()


async def test_unresolved_env_name_fails_the_attempt_with_the_name_in_the_log(
    tmp_path, caplog
) -> None:
    """A name that isn't in .env fails that attempt — the retry loop connects
    the app the moment the key lands, and the log says exactly which name."""
    env_file = tmp_path / ".env"  # never written: the key isn't anywhere
    host = MCPHost([_env_stub_config("NO_KEY_YET")], resolver=EnvResolver(env_file))
    host.start()
    try:
        with caplog.at_level(logging.WARNING, logger="aether.connectors.mcp_host"):
            assert await host.wait_all_ready(timeout=2) == {"stub": False}
        assert "NO_KEY_YET" in caplog.text
    finally:
        await host.stop()


async def test_key_added_after_boot_connects_without_restart(tmp_path) -> None:
    """The staleness fix: the resolver re-reads .env on every attempt, so a
    key added after the server started failing connects it with no restart."""
    env_file = tmp_path / ".env"
    env_file.write_text("")  # empty: the first attempts fail
    host = MCPHost([_env_stub_config("LATE_KEY")], resolver=EnvResolver(env_file))
    host.start()
    try:
        await host.wait_all_ready(timeout=2)  # not ready — the name isn't set
        await asyncio.sleep(0.1)
        env_file.write_text("LATE_KEY=fashionably-late\n")  # and now it is
        assert await host.wait_all_ready(timeout=30) == {"stub": True}
        assert await host.call("stub__env", {"name": "LATE_KEY"}) == "fashionably-late"
    finally:
        await host.stop()


async def test_expand_resolves_header_values_at_open_time(tmp_path) -> None:
    """The same _expand serves the http path: header values resolve at open
    time through the resolver, a value without a reference passes through,
    and empty means the SDK's safe defaults (None) — never a child with no
    PATH in its environment."""
    env_file = tmp_path / ".env"
    env_file.write_text("HEADER_KEY=by-name\n")
    conn = MCPServerConnection(_stub_config(), EnvResolver(env_file))
    assert await conn._expand(None) is None
    assert await conn._expand({}) is None  # empty env → the safe defaults
    assert await conn._expand({"A": "literal-x"}) == {"A": "literal-x"}
    assert await conn._expand({"Authorization": "Bearer $HEADER_KEY"}) == {
        "Authorization": "Bearer by-name"
    }


# -- a hub's URL: $NAME in transport.url, resolved at open time --------------------


def _hub_config(url: str, name: str = "hub") -> MCPServerConfig:
    return MCPServerConfig(name=name, transport=TransportConfig(type="http", url=url))


async def test_url_refs_resolve_at_open_time(tmp_path, monkeypatch) -> None:
    """A hub's URL carries the user's key inside it, so it lives in .env by
    name and is referenced exactly like env values and headers — expanded at
    every open, so a rotated key needs no restart."""
    env_file = tmp_path / ".env"
    env_file.write_text("HUB_MCP_URL=https://hub.example/mcp?key=hush-hush\n")
    captured: dict[str, str] = {}

    import mcp.client.streamable_http as streamable_http

    def fake_client(url, http_client=None):
        captured["url"] = url
        raise RuntimeError("stop before any network")

    monkeypatch.setattr(streamable_http, "streamable_http_client", fake_client)
    conn = MCPServerConnection(_hub_config("$HUB_MCP_URL"), EnvResolver(env_file))
    with pytest.raises(RuntimeError):
        async with conn._open_session():
            pass
    assert captured["url"] == "https://hub.example/mcp?key=hush-hush"


async def test_missing_url_name_fails_the_attempt_with_the_name(tmp_path) -> None:
    """Same contract as env values and headers: a name that isn't set fails
    this attempt with its name in the error — the retry loop connects the
    hub the moment the URL lands in .env."""
    env_file = tmp_path / ".env"  # never written
    conn = MCPServerConnection(_hub_config("$HUB_MCP_URL"), EnvResolver(env_file))
    with pytest.raises(SecretEnvError) as boom:
        async with conn._open_session():
            pass
    assert "HUB_MCP_URL" in str(boom.value)


# -- a header-carrying server: the http client keeps mcp's tuned timeouts ------


async def test_header_client_comes_from_mcps_own_factory(tmp_path, monkeypatch) -> None:
    """Servers with headers (a hub's x-api-key) need a custom http client —
    and it must come from mcp's own factory, which carries the transports'
    timeouts (30s general, 300s SSE read). A bare AsyncClient defaults to
    5s reads: the idle listen stream dies seconds after connect and the
    session flaps in a reconnect loop."""
    env_file = tmp_path / ".env"
    env_file.write_text("HUB_KEY=secret\n")
    captured: dict = {}

    class FakeHttpClient:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    import mcp.client.streamable_http as streamable_http
    import mcp.shared._httpx_utils as httpx_utils

    fake_http = FakeHttpClient()

    def fake_factory(headers=None, timeout=None, auth=None):
        captured["headers"] = headers
        return fake_http

    def fake_client(url, http_client=None):
        captured["http_client"] = http_client
        raise RuntimeError("stop before any network")

    monkeypatch.setattr(httpx_utils, "create_mcp_http_client", fake_factory)
    monkeypatch.setattr(streamable_http, "streamable_http_client", fake_client)

    config = MCPServerConfig(
        name="hub",
        transport=TransportConfig(
            type="http", url="https://hub.example/mcp", headers={"x-api-key": "$HUB_KEY"}
        ),
    )
    conn = MCPServerConnection(config, EnvResolver(env_file))
    with pytest.raises(RuntimeError):
        async with conn._open_session():
            pass

    assert captured["headers"] == {"x-api-key": "secret"}  # $NAME expanded as usual
    assert captured["http_client"] is fake_http
    assert fake_http.closed  # the finally still closes the client it opened


# -- _describe: a group logs its leaves, not "unhandled errors" ------------------


def test_describe_unwraps_exception_groups() -> None:
    assert mcp_host._describe(ValueError("boom")) == "ValueError: boom"

    group = ExceptionGroup(
        "unhandled errors in a TaskGroup",
        [TimeoutError("read timed out"), RuntimeError("stream closed")],
    )
    described = mcp_host._describe(group)
    assert "TimeoutError: read timed out" in described
    assert "RuntimeError: stream closed" in described
    assert "unhandled errors" not in described

    nested = ExceptionGroup("outer", [ExceptionGroup("inner", [ValueError("deep")])])
    assert "ValueError: deep" in mcp_host._describe(nested)


# -- the re-list heartbeat: a hub grows after connect ----------------------------


def _tool_list(*names: str) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(tools=[SimpleNamespace(name=n) for n in names])


def test_absorb_bumps_the_version_only_when_the_name_set_changes() -> None:
    """The version is the loop's cheap growth signal — it must move on any
    gain or loss, and stay put when nothing changed."""
    conn = MCPServerConnection(_stub_config())
    conn._absorb(_tool_list("echo", "add"))  # the first list counts as a change
    assert conn.tool_version == 1
    conn._absorb(_tool_list("echo", "add"))  # same set — no bump
    assert conn.tool_version == 1
    conn._absorb(_tool_list("echo", "add", "late"))  # a hub grew
    assert conn.tool_version == 2
    conn._absorb(_tool_list("echo"))  # a tool vanished — that counts too
    assert conn.tool_version == 3
    assert set(conn._tools) == {"echo"}


async def test_a_failed_relist_is_a_warning_not_a_crash(caplog) -> None:
    """The heartbeat asks a live session — if that ask fails, the session is
    still up and the next cadence asks again; nothing reconnects."""
    conn = MCPServerConnection(_stub_config())

    class DeadSession:
        async def list_tools(self):
            raise RuntimeError("connection lost mid-ask")

    with caplog.at_level(logging.WARNING, logger="aether.connectors.mcp_host"):
        await conn._relist(DeadSession())
    assert "re-list failed" in caplog.text


async def test_a_live_session_relists_so_growth_reaches_the_specs(monkeypatch) -> None:
    """The hub story end to end: a server that gains a tool after connect
    shows it in the host's specs within one cadence — no reconnect, no
    restart — and the version moves so the loop can say so out loud."""
    monkeypatch.setattr(mcp_host, "_RELIST_SECONDS", 0.2)
    host = MCPHost([_stub_config()])
    host.start()
    try:
        assert await host.wait_all_ready(timeout=30) == {"stub": True}
        conn = host.connections[0]
        version = conn.tool_version
        assert "stub__late" not in {s.name for s in host.tool_specs()}

        await host.call("stub__grow", {})  # the server grows, the session lives on

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if "stub__late" in {s.name for s in host.tool_specs()}:
                break
            await asyncio.sleep(0.05)
        assert "stub__late" in {s.name for s in host.tool_specs()}
        assert conn.tool_version == version + 1
        assert await host.call("stub__late", {}) == "i grew"
    finally:
        await host.stop()
