from __future__ import annotations

import pytest

from aether.vault.errors import VaultProtocolError
from aether.vault.registry import (
    DEDUP_DOMAINS,
    TOKEN_DOMAINS,
    resolve_dedup,
    resolve_token,
)
from aether.vault.schema import CATEGORIES


def test_every_domain_names_a_real_category_and_namespace() -> None:
    for name, entry in {**TOKEN_DOMAINS, **DEDUP_DOMAINS}.items():
        assert entry.cat in CATEGORIES, name
        assert entry.ver >= 1, name
        assert entry.namespace, name


def test_namespaces_are_unique_per_category_and_version() -> None:
    seen: dict[tuple[str, int, str], str] = {}
    for name, entry in {**TOKEN_DOMAINS, **DEDUP_DOMAINS}.items():
        key = (entry.cat, entry.ver, entry.namespace)
        assert key not in seen, f"{name} reuses {seen.get(key)}"
        seen[key] = name


def test_token_and_dedup_domains_do_not_overlap() -> None:
    assert not set(TOKEN_DOMAINS) & set(DEDUP_DOMAINS)


def test_resolve_returns_the_registered_entry() -> None:
    assert resolve_token("event.source") is TOKEN_DOMAINS["event.source"]
    assert resolve_token("identity.platform") is TOKEN_DOMAINS["identity.platform"]
    assert resolve_dedup("event.dedup") is DEDUP_DOMAINS["event.dedup"]


def test_unknown_domains_are_rejected_with_a_closed_code() -> None:
    with pytest.raises(VaultProtocolError) as token_error:
        resolve_token("events.source")
    assert token_error.value.code == "bad_field"
    with pytest.raises(VaultProtocolError) as dedup_error:
        resolve_dedup("event.source")
    assert dedup_error.value.code == "bad_field"
