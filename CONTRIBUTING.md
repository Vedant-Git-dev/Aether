# Contributing to Aether

Thanks for helping build a personal agent people can actually trust.

## Getting set up

```bash
git clone https://github.com/Vedant-Git-dev/Aether.git
cd Aether
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

You need Python 3.12 or newer. Node 22 is only needed to build or develop the panel.

## Running the tests

```bash
pytest -m "not integration and not e2e"     # unit tests: no network, no database
ruff check src tests
ruff format --check .
```

Integration tests need a throwaway Postgres. They truncate every table they touch, so
never point them at a database you care about:

```bash
createdb aether_scratch_ci
AETHER_TEST_DATABASE_URL=postgresql://localhost/aether_scratch_ci pytest -m integration
dropdb aether_scratch_ci
```

Browser tests need Chromium and a built panel:

```bash
playwright install chromium
cd webapp && npm ci && npm run build && cd ..
AETHER_TEST_E2E=1 pytest -m e2e
```

Unit tests must stay hermetic: no network, no database, no API keys. Anything touching a
real provider or Postgres goes behind the `integration` marker. `tests/fakes.py` holds the
in-memory doubles; extend those rather than mocking SDKs ad hoc.

The `e2e` suite drives the real frontend against `tests/e2e/dev_server.py`, which is the
real app wired to in-memory fakes. Extend that fixture and `tests/e2e/test_smoke.py` when
you change frontend behavior. Do not verify frontend changes by screenshots alone.

For hands-on panel work, `npm run dev` proxies `/api` to a locally running `aether` on
port 8000. To click through the fixture instead:

```bash
python tests/e2e/dev_server.py
AETHER_DEV_API=http://127.0.0.1:8731 npm run dev:test
```

`dev:test` skips unless `AETHER_DEV_API` is set, so a test run never silently targets the
real backend.

## Quickstart check

```bash
bash scripts/check_quickstart.sh --skip-install
```

`check_quickstart.sh` runs the documented no-database quick start against the example
config and the fixture server.

## Code conventions

- Python 3.12+, async everywhere. The service is one asyncio process.
- `ruff check` and `ruff format --check` must pass; both are configured in
  `pyproject.toml`.
- Plain SQL through asyncpg; no ORM. Migrations are forward-only files in
  `src/aether/migrations/`, applied in filename order.
- Anything private is encrypted before it touches the database (`aether.memory.crypto`)
  and bound to its record with additional authenticated data.
- The authorization layer stays deterministic: no LLM in the decision path, only code and
  declared rules.
- Every connector failure degrades gracefully. One broken MCP server or bot token must
  never take the agent loop down.
- No emojis in user-facing strings, comments, logs, documentation, or commit messages.
- The panel shows plain language, never raw tool names, rule ids, event kinds, ISO
  timestamps, or JSON. When a new backend string shape appears, add a mapping in
  `webapp/src/lib/describe.js`; never add a raw `{value}` fallback.

## Pull requests

- Keep the subject one line, Conventional Commits style (`feat`, `fix`, `chore`, `docs`).
- Explain what changed and why. Include the verification commands you ran.
- Add or update tests for behavior changes.
- Update the relevant `docs/` page when a configuration field, command, channel, or
  user-visible behavior changes.
- Do not include generated panel assets (`src/aether/web/` is built in CI).
- Do not add attribution trailers of any kind.

CI runs ruff, the unit suite on Python 3.12 and 3.14, the integration suite against
Postgres, the panel build, the browser suite, and the documentation checks. A PR is ready
when CI is green.

## Reporting issues

Open an issue with:

- what you did,
- what you expected,
- what happened,
- the relevant audit entries from `/api/audit`, with secrets removed.

For security problems, follow [SECURITY.md](SECURITY.md) instead of opening a public
issue.
