"""Shared fixtures.

Unit tests stay hermetic (no network, no database). Integration tests are
opt-in: they run only when AETHER_TEST_DATABASE_URL is set, and manage a
migrated, truncated database themselves.
"""

from __future__ import annotations

import os

import pytest

from aether.memory.db import create_pool, run_migrations

DB_URL = os.environ.get("AETHER_TEST_DATABASE_URL", "")

# Tables integration tests are allowed to wipe between tests.
_TABLES = (
    "audit_log, pending_approvals, events, identities, identity_handles, "
    "entity_notes, chat_messages, scheduled_actions"
)


def pytest_collection_modifyitems(config, items):
    if DB_URL:
        return
    skip = pytest.mark.skip(reason="integration: set AETHER_TEST_DATABASE_URL to run")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
async def db():
    """A migrated, freshly-truncated pool. Requires AETHER_TEST_DATABASE_URL."""
    pool = await create_pool(DB_URL)
    await run_migrations(pool)
    await pool.execute(f"TRUNCATE {_TABLES} RESTART IDENTITY")
    yield pool
    await pool.execute(f"TRUNCATE {_TABLES} RESTART IDENTITY")
    await pool.close()
