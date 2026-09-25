# Contributing to Aether

Thanks for helping build a personal agent people can actually trust.

## Getting set up

```bash
git clone <your-fork>
cd aether
cp .env.example .env        # you only need real values for a live smoke test
cp config.example.yaml config.yaml
pip install -e ".[dev]"
```

## Running the tests

```bash
pytest                                  # unit tests — no network, no database
AETHER_TEST_DATABASE_URL=<url> pytest -m integration   # opt-in, needs Postgres
```

Unit tests must stay hermetic: no network calls, no database, no API keys.
Anything touching a real provider or Postgres goes behind the `integration`
marker. `tests/fakes.py` holds the in-memory provider/connector doubles —
extend those rather than mocking SDKs ad hoc.

## Code conventions

- Python 3.12+, async everywhere — the whole service is one asyncio process.
- Plain SQL through asyncpg; no ORM. Migrations are forward-only files in
  `src/aether/migrations/`.
- Anything a human would consider private gets encrypted before it touches
  the database (`aether.memory.crypto`) and is bound to its record via AAD.
- The authorization layer stays **deterministic**: no LLM in the decision
  path, only code and declared rules.
- Every connector failure degrades gracefully — one broken MCP server or bot
  token must never take the agent loop down.

## Commit style

Small, reviewable commits with a clear subject line. Tests accompany every
behavior change.

## Reporting issues

Open an issue with: what you did, what you expected, what happened, and the
relevant entries from the audit log (`/api/audit`).
