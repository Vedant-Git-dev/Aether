"""The model catalog: what the Settings page's dropdowns list, fetched
per provider from the provider's own models endpoint, keyed to the
configured keys. No network — the SDK clients are fakes; the house rule
under test throughout: a key's value never rides anywhere, and one
provider's failure never breaks the list.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from aether.llm.model_list import ModelCatalog


class FakeResolver:
    """Resolves by name from a dict — the paste/env/.env seam's double."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}

    async def resolve(self, name: str) -> str | None:
        return self.values.get(name)


def _anthropic_client(ids: list[str]):
    # the real SDK's models.list() hands back the paginator itself, no
    # await — the fake speaks the same shape
    def _list():
        async def gen():
            for i in ids:
                yield SimpleNamespace(id=i)

        return gen()

    return SimpleNamespace(models=SimpleNamespace(list=_list))


def _openai_client(ids: list[str]):
    async def _list():
        return SimpleNamespace(data=[SimpleNamespace(id=i) for i in ids])

    return SimpleNamespace(models=SimpleNamespace(list=_list))


def _gemini_client(names: list[str]):
    async def _list():
        async def gen():
            for n in names:
                yield SimpleNamespace(name=n)

        return gen()

    return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(list=_list)))


def _client_for(clients: dict[str, object], calls: list[str]):
    """The injectable factory: hands each provider its fake and records
    every fetch, so cache behavior is assertable."""

    def factory(provider: str, secret: str) -> object:
        calls.append(provider)
        maker = clients[provider]
        if isinstance(maker, Exception) or (
            isinstance(maker, type) and issubclass(maker, Exception)
        ):
            raise maker
        return maker

    return factory


async def test_two_configured_providers_list_their_models_together() -> None:
    # the dropdown's acceptance shape: models from more than one
    # provider in one feed, each group under its provider's name
    catalog = ModelCatalog(
        FakeResolver({"ANTHROPIC_API_KEY": "sk-a", "GEMINI_API_KEY": "g"}),
        client_for=_client_for(
            {
                "anthropic": _anthropic_client(["claude-opus-5", "claude-haiku-4-5"]),
                "gemini": _gemini_client(["models/gemini-2.5-flash", "models/gemini-3.1-flash"]),
            },
            calls=[],
        ),
    )
    feed = await catalog.feed()
    assert feed["configured"] is True
    groups = {g["provider"]: g for g in feed["providers"]}
    assert groups["anthropic"]["models"] == ["claude-haiku-4-5", "claude-opus-5"]
    # Gemini's endpoint names its models "models/<id>" — the id is what
    # follows the prefix, and that is what the stored value will carry
    assert groups["gemini"]["models"] == ["gemini-2.5-flash", "gemini-3.1-flash"]
    assert all(g["error"] == "" for g in feed["providers"])


async def test_one_providers_failure_never_breaks_the_list() -> None:
    calls: list[str] = []
    catalog = ModelCatalog(
        FakeResolver({"ANTHROPIC_API_KEY": "sk-a", "OPENAI_API_KEY": "sk-o"}),
        client_for=_client_for(
            {
                "anthropic": _anthropic_client(["claude-opus-5"]),
                "openai": RuntimeError("down"),
            },
            calls=calls,
        ),
    )
    feed = await catalog.feed()
    groups = {g["provider"]: g for g in feed["providers"]}
    assert groups["anthropic"]["models"] == ["claude-opus-5"]
    assert groups["anthropic"]["error"] == ""
    # the failure is an inline note on its own group, in plain words
    assert groups["openai"]["models"] == []
    assert "couldn't be reached" in groups["openai"]["error"]


async def test_the_list_is_cached_until_the_key_changes_or_refresh_is_asked() -> None:
    calls: list[str] = []
    resolver = FakeResolver({"ANTHROPIC_API_KEY": "sk-old"})
    catalog = ModelCatalog(
        resolver, client_for=_client_for({"anthropic": _anthropic_client(["m1"])}, calls)
    )
    await catalog.feed()
    await catalog.feed()  # same key — the cache answers, no second fetch
    assert calls == ["anthropic"]
    await catalog.feed(refresh=True)  # a manual refresh refetches
    assert calls == ["anthropic", "anthropic"]
    resolver.values["ANTHROPIC_API_KEY"] = "sk-new"
    await catalog.feed()  # the key changed — the cache refetches
    assert calls == ["anthropic", "anthropic", "anthropic"]


async def test_a_failed_fetch_is_not_cached() -> None:
    # a transient blip heals on the next open instead of sticking — only
    # a real result is a cacheable result
    calls: list[str] = []
    clients: dict[str, object] = {"anthropic": RuntimeError("down")}
    resolver = FakeResolver({"ANTHROPIC_API_KEY": "sk-a"})
    catalog = ModelCatalog(resolver, client_for=_client_for(clients, calls))
    first = await catalog.feed()
    assert first["providers"][0]["models"] == []
    clients["anthropic"] = _anthropic_client(["claude-opus-5"])
    second = await catalog.feed()
    assert second["providers"][0]["models"] == ["claude-opus-5"]
    assert calls == ["anthropic", "anthropic"]


async def test_a_removed_key_takes_its_provider_off_the_list() -> None:
    resolver = FakeResolver({"ANTHROPIC_API_KEY": "sk-a", "OPENAI_API_KEY": "sk-o"})
    catalog = ModelCatalog(
        resolver,
        client_for=_client_for(
            {"anthropic": _anthropic_client(["m1"]), "openai": _openai_client(["m2"])},
            calls=[],
        ),
    )
    assert len((await catalog.feed())["providers"]) == 2
    del resolver.values["OPENAI_API_KEY"]
    feed = await catalog.feed()
    assert [g["provider"] for g in feed["providers"]] == ["anthropic"]
    # the dropped provider's cache went with it — the key coming back
    # refetches rather than serving the stale list
    resolver.values["OPENAI_API_KEY"] = "sk-o2"
    assert len((await catalog.feed())["providers"]) == 2


async def test_no_keys_configured_is_the_empty_state() -> None:
    catalog = ModelCatalog(FakeResolver({}), client_for=_client_for({}, calls=[]))
    feed = await catalog.feed()
    assert feed == {"configured": False, "providers": []}


async def test_ollama_lists_through_its_address(caplog) -> None:
    # Ollama has no key — its address is what makes it configured, and it
    # speaks the OpenAI models shape
    calls: list[str] = []
    catalog = ModelCatalog(
        FakeResolver({"OLLAMA_BASE_URL": "http://localhost:11434/v1"}),
        client_for=_client_for({"ollama": _openai_client(["qwen2.5vl:7b", "llama3.3"])}, calls),
    )
    feed = await catalog.feed()
    assert feed["configured"] is True
    assert feed["providers"] == [
        {"provider": "ollama", "models": ["llama3.3", "qwen2.5vl:7b"], "error": ""}
    ]


async def test_a_key_value_never_rides_the_feed_or_the_log(caplog) -> None:
    calls: list[str] = []
    secret = "sk-never-echo-this"
    resolver = FakeResolver({"ANTHROPIC_API_KEY": secret, "OPENAI_API_KEY": "sk-o"})
    catalog = ModelCatalog(
        resolver,
        client_for=_client_for(
            {"anthropic": _anthropic_client(["m1"]), "openai": RuntimeError("down")},
            calls,
        ),
    )
    with caplog.at_level(logging.WARNING, logger="aether.llm.model_list"):
        feed = await catalog.feed()
    assert feed["providers"][1]["error"]  # the failure is noted, plainly
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert secret not in logged
    assert secret not in repr(feed)
