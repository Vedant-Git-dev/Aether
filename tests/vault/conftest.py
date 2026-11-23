import pytest

from aether.vault import schema


@pytest.fixture(autouse=True)
def fixture_use_cases(monkeypatch):
    # These IDs only exercise the wire schema. Production starts fail-closed
    # until a shared registry of typed entry points has been approved.
    for operation, categories in schema.OPERATION_CATEGORIES.items():
        monkeypatch.setitem(schema.USE_CASES, operation, (operation, categories))
