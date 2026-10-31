# AGENTS.md

Standing instructions for any coding agent working in the Aether repository. Sections link to each other with standard Markdown anchors, so they render as navigable cross-references in any viewer.

Contents:
- [Hard rules](#hard-rules)
- [Git and commits](#git-and-commits)
- [Environment and data safety](#environment-and-data-safety)
- [Verification chain](#verification-chain)
- [Commit handoff format](#commit-handoff-format)
- [Workflow](#workflow)
- [UI rules](#ui-rules)
- [Performance work](#performance-work)
- [Project status](#project-status)

## Hard rules

1. Never run `git add` or `git commit`. See [Git and commits](#git-and-commits).
2. Never touch the live `aether` Postgres database. See [Environment and data safety](#environment-and-data-safety).
3. Never read the user's `.env` or `config.yaml`.
4. Never use emojis anywhere. See [UI rules](#ui-rules).
5. Never cap LLM `max_tokens` or platform character limits. See [Performance work](#performance-work).
6. Never add Co-Authored-By or any other agent or AI attribution to commits.
7. Slack has been removed. Do not reintroduce it. See [Project status](#project-status).

## Git and commits

- Do not run `git commit` or `git add`. Read-only inspection (`git status`, `git diff`) is fine and expected: check what is ready, but never stage.
- After completing a stage of work, hand the user a paste-ready block containing both the add and the commit command, for example:

  ```bash
  git add webapp/src tests/e2e && git commit -m "feat(llm): add multi-provider LLM layer with manual tool-use loop"
  ```

- Commit message format: Conventional Commits, one-line subject only, with a prefix (`feat`, `fix`, `chore`, and so on).
- No attribution trailers. Never append `Co-Authored-By:` lines or any tool or agent attribution, even if your tooling adds one by default. Plain messages only.
- Why: the user wants to make every commit themselves. They rejected commit attempts on 2026-09-25 and a `git add` on 2026-10-10.

The handoff itself must follow [Commit handoff format](#commit-handoff-format).

## Environment and data safety

- Use `pip` and `python -m pytest`. Never use `uv`.
- Never read the user's actual `.env` or `config.yaml`; they hold personal secrets and settings. Work from the pydantic models in `src/aether/config.py` and from `config.example.yaml`.
- The local `aether` Postgres database is the user's live data (chat history, memory, approvals). Never read it, write to it, or run tests against it. The test suite truncates every table it touches (see `tests/conftest.py`).
- Integration tests are opt-in via `AETHER_TEST_DATABASE_URL` and skip by default, so a plain `pytest` run is always safe. For DB-backed verification use a scratch database:

  ```bash
  createdb aether_scratch_ci
  AETHER_TEST_DATABASE_URL=postgresql://localhost/aether_scratch_ci python -m pytest -m integration
  dropdb aether_scratch_ci
  ```

- Local Postgres accepts connections as the current user (`psql -d postgres`).

## Verification chain

Run before assuming any task is complete. Do not skip steps, and always run `ruff format --check .` and all tests before declaring done.
Run this chain after completing work and is about to hand commit and with  best possible, suitable syntax. DO NOT run it when work is left, you often run it and then again start remaining work. When work is finished AND is about to hand commit, run this

1. ruff check
2. ruff format --check and reformat if says
3. pytest with Scratch-DB suite (see [Environment and data safety](#environment-and-data-safety)): create `aether_scratch_ci`, set `AETHER_TEST_DATABASE_URL` and run full suit , drop the DB.
4. End to end: AETHER_TEST_E2E=1 and e2e tests

Pyright is not part of the chain. It was removed on 2026-10-19 because the user said it does not add value. Do not run it, do not report its results, and treat editor pyright diagnostics as noise. The old 5-error budget in `loop.py` is moot.

## Commit handoff format

Every completed build or commit handoff contains two parts:

1. The usual verification summary (the results of the [Verification chain](#verification-chain)).
2. A short section titled "What this commit added, in brief": one line each for new tables, classes, tools, flows or config, the behavior they add, and any real bugs caught while verifying. Keep it brief.

Then the paste-ready add and commit block from [Git and commits](#git-and-commits). Why: the user asked for this after Build A of the multimodal plan (2026-10-18), because a bare "tests passed" summary does not show what landed.

## Workflow

- Do all exploration, helper-agent launches and design work first, as ordinary read-only work.
- Switch into any formal planning mode only at the exact moment the final plan is ready to be written, then submit it for approval. Planning mode is a write-the-plan gate, not a container for the whole workflow.

## UI rules

- No emojis, anywhere: not in text Aether sends to the user, not in user-facing strings, comments or logs, not in replies, and not in commit messages. Strip existing emoji from user-facing strings when you touch them (for example "Approve" buttons and status prefixes) and flag emoji in code reviews.
- Plain language only in the panel. The user directive (2026-10-10): the UI should never show raw tools or JSON; it should show plain language, with names where applicable. The panel must never render raw tool names, rule ids, event kinds, ISO timestamps or JSON.

How the plain-language contract is implemented:

- The backend authors the plain sentence. Every traced call carries `plain` (the model's `_plain` argument, stripped before the tool runs, or the vocabulary in `tools.py` via `describe_call`). Agent-outcome events carry `plain` via `plain_outcome`.
- `api/routes.py` backfills `plain` at read time for legacy rows (`with_plain_calls`, `with_plain_event`). Never migrate stored data.
- The panel prefers `plain`, scrubbed through `plainText`, because model text is untrusted. It falls back to `webapp/src/lib/describe.js` (`describeTool`, `describeRule`, `describeOutcome`, `describeEvent`, `describeObservation`, `describeTrigger`, `paramRows`, with `humanize` as the last resort).
- Model-authored strings (reasoning lines, replies, parked-call instructions, tool-result text) go through `plainText()`. Tool results go through `summarizeResult()`, which unwraps composio envelopes (`{successful, data, error, log_id}`) and never quotes raw JSON.
- When a new backend string shape appears (check `src/aether/agent/loop.py` trace payloads, `authz/policy.py` reasons, `authz/approvals.py` outcomes), add a mapping to `describe.js`. Never add a raw `{value}` fallback.
- Keep `tests/e2e/dev_server.py` fixture shapes byte-faithful to the backend (the `_event_line` raw log-line format, `approval #N - <tool>` carry_out labels, trigger dicts, note payloads `{kind, note, handle, platform}`, agent action_done events with raw JSON `detail`). Otherwise the panel is only tested against sanitized data.
- The contract is machine-checked by `tests/e2e/test_plain_language.py` (a RAW_PATTERNS scan of every page and expanded trace, plus specific plain-rendering assertions).
- The friend-ownership rule for the web UI was lifted on 2026-10-03: `src/aether/web/*` may be edited directly.

## Performance work

- Never reduce LLM `max_tokens` (currently 16000) and never introduce or trim platform message character caps to cut latency or cost. Leave connector message-splitting alone. The user said so directly on 2026-10-25: do not cap the LLM max_tokens or any platform's chars.
- Latency fixes must come from parallelism, batching, or moving work off the reply path.

Latency report status (see [Project status](#project-status)):

- Shipped 2026-10-25 on branch `dev`: fixes 1, 4, 7, 8 (salience scoring off the solicited reply path via `_spawn_scoring` in `loop.py`; due source polls deferred past a waiting message's turn via the `polled` flag in `_tick`; concurrent tool calls per proposal in `agent.py` with same-batch exact-duplicate dedup; concurrent attachment processing in `attachments.py`). Do not re-propose these.
- Open, deliberately not picked: 2 (reply streaming), 3 (Neon/pooler/infra), 5 (vector recall cache), 6 (embedding 429 caps/backfill yield), 9 (SurfaceFanout gather), 10 (panel/API read paths), 11 (shared httpx client). Start from this list if latency work resumes, and do not implement any of them unless asked.
- Non-obvious fact: on this project's Python 3.14 the eager task factory means `asyncio.wait_for` does not suspend when the wrapped coroutine never yields (FakeProvider turns run synchronously inside it). The tick-tail `asyncio.sleep(0)` is load-bearing: it gives spawned scorers their first step so none is left pending at loop close, and the never-edit suite passes because of it. Do not remove it.

## Project status

- Composio: the reintroduction plan is done and live-verified on 2026-10-08 (Gmail fetch worked; a send parked at the approval gate and ran after one-tap approval; session reported 61 actions). Treat Composio as the only connection mechanism and do not resume the plan. Cosmetic quirks seen in the smoke test, to raise only if the user complains: the "composio is up - 61 actions" status line is re-sent on unrelated messages, the agent answered the time in UTC rather than local time, and raw tool-result JSON is relayed verbatim into chat (the last one is also governed by [UI rules](#ui-rules)).
- Slack: removed on 2026-10-29 at the user's request. Do not reintroduce the connector, its tokens, tests, fixtures, docs, or UI. Historical Slack rows may still exist in old traces and audit records; keep the legacy display mappings in `webapp/src/lib/describe.js` so those rows stay readable.
