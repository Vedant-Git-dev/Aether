# Database behavior

Aether keeps its database work deliberately quiet: an idle process performs
no queries, and only optional background work is ever paused. This note is
for contributors changing a loop, a store or a panel list. The user docs
describe setup and settings; they do not need this detail.

## Idle means no queries

- The agent tick wakes every 30 seconds but reads nothing unless a dirty
  flag, a new message, a due job or an explicit wake says there is work.
- Expiry, delivery checks, stale-inbound checks, reminders and observation
  reads run in one background burst every `db.background_window_minutes`
  (default 15). An interactive message never waits for the window.
- Provider poll cursors and state, the scheduler due time and the MCP poll
  schedule live in memory. They are read once and written only when a value
  changes; a poll that finds nothing writes nothing and runs no query.
- The scheduler claims work in memory and only touches the database for the
  jobs it actually runs.
- The vector backfill runs at boot, immediately after a failed index pass,
  and otherwise once every 15 minutes (`BACKFILL_SECONDS` in
  `src/aether/agent/loop.py`).
- Audit-chain verification is a full walk, but only when the audit view is
  opened or refreshed. It is not a timer.

## Reads and caches

- Panel lists are pulled, not polled: the page loads once, then only on
  Refresh. Responses carry an ETag, so an unchanged list returns a 304 with
  no body, and one page is capped at 100 rows (`PANEL_PAGE_SIZE` in
  `src/aether/api/routes.py`).
- Stores select the columns they use instead of `SELECT *`.
- Vector recall ranks over a resident matrix and then fetches text and
  metadata only for the top results by id. The matrix loads at boot in
  1,000-row batches (`CACHE_BATCH` in `src/aether/memory/vectors.py`) and
  keeps at most `memory.vector_cache_max_chunks` chunks (default 8,000).
  Hitting the cap drops the oldest chunk from the cache, not from the
  database; the existing chunk listener keeps the cache current without a
  rescan.
- The lexical and hot-event indexes are resident and follow the same chunk
  writer.

## Optional-work budget

`DailyBudget` reads the process-local `QueryMeter`, which counts calls and
estimates response bytes for the current UTC day. Only the vector backfill
consults it: when the day reaches `db.daily_query_budget` or
`db.daily_response_budget_mb`, the backfill pauses with zero queries and
resumes on the next UTC day. Interactive messages, polls and panel reads
never consult the budget, so it can never delay a reply.

## The shared pool

One pool per process, with a zero floor, at most five connections, and a
60-second idle lifetime. An idle process therefore releases every
connection instead of holding the database awake. These are constants in
`src/aether/memory/db.py`.

## When the database is unavailable

A circuit breaker wraps the pool. After three consecutive connection
failures it opens; from then on every call raises `DatabaseUnavailable`
immediately, with no network wait. The first probe wait is two seconds,
doubling with jitter on every failed probe up to five minutes. One success
closes the circuit. A SQL error never trips it — only connection refusals,
dropped connections, a closed pool and timeouts count.

While the circuit is open:

- The agent loop sends one plain sentence when the circuit opens and one
  when it closes. The notice itself opens no delivery row.
- Messages that arrive stay queued in memory. The loop returns before
  draining them, so they are answered in order after recovery rather than
  dropped or invented.
- Approvals, audit appends, memory reads and writes, provider polls and
  database-backed panel lists fail closed. No action runs and no audit entry
  is invented without the database to record it.

Recovery is automatic on the next successful probe. Do not restart the
process to hurry it; a boot that cannot reach the database fails by design.

## Settings

Only these are operator-facing:

| Key | Default | Meaning |
| --- | --- | --- |
| `db.background_window_minutes` | `15.0` | non-urgent background burst cadence |
| `db.daily_query_budget` | `2500` | optional-work pause point, queries per UTC day |
| `db.daily_response_budget_mb` | `120.0` | optional-work pause point, estimated MB per UTC day |
| `memory.vector_cache_max_chunks` | `8000` | most chunks the resident vector matrix keeps |

Pool size and idle lifetime, breaker thresholds, the backfill retry
interval, the panel page cap and the cache load batch are constants next to
the code they govern.
