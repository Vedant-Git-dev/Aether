---
title: "Database budget"
summary: "How Aether measures its database work, what the first measurement found, and the plan for keeping a free-tier database inside its limits."
read_when:
  - You are running Aether on a metered Postgres plan
  - You want to read `aether db-stats` or the Database Use page
  - You are changing a background loop or a panel list
---

# Database budget

Aether treats the database as a metered resource. The application is one
long-running process, and a 30-second agent tick used to mean a query every
30 seconds around the clock — enough that a free-tier compute instance never
reached its idle window and ran continuously. This page records how the cost
is measured, what the first measurement found, and the rules that keep it
down.

**Every number on this page is an estimate from the local harness, not a
reading of a provider's meter.** The harness runs against a scratch Postgres
database with synthetic data. It never reads the live database.

## Reading the meters

- The panel's **Database Use** page shows query counts, estimated response
  sizes, the busiest parts of Aether, the most-read tables, an hourly
  pattern and the outage circuit's state. It loads once when opened and
  again only when you press Refresh.
- `aether db-stats` prints the same snapshot in a terminal. The counters live
  inside the running server, so the command talks to it over localhost:

  ```bash
  AETHER_TOKEN=... aether db-stats
  ```

- The meters hold counts, query shapes, timings and call sites. They never
  hold query parameters, rows or any user content, and they are memory-only.
  No new table, file or log is created for them.

## What the replay measures

`python -m aether.db_replay --url postgresql:///your_scratch_database` seeds
synthetic history and walks one simulated day: agent ticks, scheduler claims,
provider checks, source polls, messages with recall, mail arrivals, panel
reads and a restart. It reports queries, rows, estimated response bytes and
active compute time.

Active time is a model: a query wakes compute, and compute stays awake for the
provider's idle window (five minutes on the free Neon tier) after the last
query. The union of those windows is the estimate. It is not a provider
reading.

The harness refuses any database whose name does not contain `scratch`,
`test`, `ci` or `replay`, and it refuses `aether` outright: it truncates every
application table.

## Baseline, before the fixes

One simulated day on a 15.3 MB scratch database, with 20 messages, 12 mails,
2,880 agent ticks, 4,320 scheduler claims, one restart and a panel left open
for two hours:

| Measure | Result |
| --- | --- |
| Queries | 30,987 |
| Rows returned | 337,849 |
| Estimated response bytes | 168.5 MB |
| Active compute | 24.0 hours |
| Projected compute | 180 CU-hours / month |
| Projected transfer | 5.05 GB / month |

The provider's free tier allows 100 CU-hours and 5 GB per month, so the
baseline is over on compute by 80 percent and at the transfer limit. The
observed production figure of about 67 CU-hours in 12 days is consistent
with a compute that never suspends: 0.25 CU times 24 hours is 6 CU-hours a
day, or about 180 a month.

### The largest readers

| Code path | Calls | Rows | Estimated bytes |
| --- | --- | --- | --- |
| Vector recall (`VectorMemory.search_vector`) | 20 | 44,780 | 53.3 MB |
| Panel audit list | 720 | 144,000 | 39.2 MB |
| Panel trace list | 720 | 36,000 | 34.2 MB |
| Panel event list | 720 | 72,000 | 27.1 MB |
| Open commitments, every tick | 2,880 | 34,560 | 11.3 MB |

### The largest query counts

| Code path | Calls | Why it ran |
| --- | --- | --- |
| Reply-delivery checks | 5,760 | twice on every 30-second tick |
| Scheduler claim | 4,321 | every 20 seconds |
| Open commitments | 2,880 | every 30-second tick |
| Approval expiry | 2,880 | every 30-second tick |
| Stale inbound check | 2,880 | every 30-second tick |
| Observation stream | 2,880 | every 30-second tick |
| Vector index backfill poll | 1,440 | every minute, even when caught up |
| Panel approval poll | 1,440 | every 5 seconds while open |

Every one of these is shorter than the five-minute idle window, so compute
never suspends for the whole day.

## After Phase 1

Same synthetic day, same seed, same 20 messages and 12 mails. The panel is
closed (its pollers are gone) unless noted.

| Measure | Before | After |
| --- | --- | --- |
| Queries | 30,987 | 1,300 |
| Rows returned | 337,849 | 51,009 |
| Estimated response bytes | 168.5 MB | 19.0 MB |
| Active compute | 24.0 h | 8.8 h |
| Projected compute | 180 CU-hours / month | 65.8 CU-hours / month |
| Projected transfer | 5.05 GB / month | 0.57 GB / month |

With an explicit panel visit (two page loads, no polling) the day is 1,308
queries and 19.2 MB. The budgets are now met: queries 1,308 of 2,500,
response 19.2 of 120 MB, active 526 of 600 minutes.

What was left after Phase 1 was mostly content, not polling:

- Vector recall still scanned every chunk (40 calls, 45,360 rows, 15.6 MB).
  The Phase 2 matrix removes it.
- The remaining active time is the 15-minute background bursts plus the
  messages themselves.
- Reply-delivery checks (232), observation reads (126), open commitments
  (96), expiry (96), stale-inbound (96) and backfill (96) each run once per
  background burst, not once per 30-second tick.

## After Phase 2

The same synthetic day, with the same two-hour panel visit.

| Measure | Phase 1 | Phase 2 |
| --- | --- | --- |
| Queries | 1,308 | 1,291 |
| Rows returned | 51,509 | 8,969 |
| Estimated response bytes | 19.2 MB | 4.6 MB |
| Active compute | 8.8 h | 8.8 h |
| Projected compute | 65.8 CU-hours / month | 65.8 CU-hours / month |
| Projected transfer | 0.57 GB / month | 0.14 GB / month |

**The vector matrix.** At boot, `VectorMemory.load_cache` reads ids and sealed
vectors only, newest first, in `memory.cache_batch` batches (1,000 rows). It
ran three times for this seed's 2,220 chunks: 2,220 rows, about 759 KB on the
wire. The process then holds a `float32` matrix of 768 dimensions — about
6.8 MB for this corpus, roughly double while the staging rows and the built
matrix are both held — and ranking is one in-process matrix-vector product.
A recall fetches only the text and metadata of its top-k by id: 20 recalls
read 600 rows and 252 KB, down from 45,360 rows and 15.6 MB.

The matrix is capped at `memory.vector_cache_max_chunks` (8,000 chunks). When
the cap is reached the oldest chunks leave the cache, not the database; they
stay reachable by the lexical index, and the Database Use page reports the
cap. Adding a chunk is incremental through the existing chunk listener, so a
new message is searchable without any scan.

**Optional-work budget.** The backfill checks `DailyBudget`, which reads the
same in-process meters as the panel. When the current UTC day's measured use
has reached `daily_query_budget` or `daily_response_budget_mb`, the backfill
pauses with zero queries and resumes after the UTC day rolls over.
Interactive messages, polls and panel reads never consult it.

What is left is the content path itself: message recall and ingestion, open
commitments (96 reads), delivery checks (232), and the 15-minute background
bursts. Active time is unchanged at 8.8 h; Phase 3 is about behavior under
pressure, not more idle gains.

## Hypotheses, checked against the code

1. **Recall reads and decrypts every chunk on each solicited turn.** Confirmed.
   `VectorMemory.search_vector` selects `text_enc`, `embedding_enc` and
   `meta_enc` for every chunk of the embedding model
   (`src/aether/memory/vectors.py:442-446`), although only the top-k rows need
   their text decrypted (`vectors.py:468-476`). In the replay, 20 recalls read
   44,780 rows and 53 MB.
2. **The embedding backfill loop.** Confirmed as an idle-wake source. When
   caught up it issues one `SELECT ... WHERE indexed_at IS NULL` every 60
   seconds forever (`src/aether/agent/loop.py:872-884`,
   `src/aether/memory/vectors.py:359-373`). It is cheap in bytes, but 60
   seconds is below the idle window.
3. **Polling loops that query or write when nothing changed.** Confirmed.
   - The provider poll reads every connection row and both cursor values
     before it can know whether a poll is due
     (`src/aether/providers/runtime.py:620-653`), then writes three poll-state
     rows on every due poll even when the result is empty
     (`runtime.py:668`, `runtime.py:703-724`). The poll runs on the 30-second
     tick.
   - The scheduler claims work every 20 seconds by reading every pending row
     with its sealed payload (`src/aether/scheduler/jobs.py:108-133`,
     `scheduler_worker` interval at `jobs.py:212`).
   - Source polls write an audit row and ingest a poll event even when the
     result is unchanged (`src/aether/agent/loop.py:2002-2059`).
   - The alert tick reads pending notifications and the day's sent count every
     tick (`src/aether/alerts/engine.py:178-184`).
   - Commitment reminders read the whole open set every tick
     (`src/aether/proactive/path.py:182-204`).
   - Delivery checks read the failed and queued rows twice per tick
     (`src/aether/agent/loop.py:1432-1433`,
     `src/aether/memory/deliveries.py:141-152`).
4. **The uptime ping hits the database.** Rejected. `/healthz` and `/health`
   return a literal dictionary and never touch the pool
   (`src/aether/main.py:900-902`); the Render health check points there.
5. **Panel pages auto-refresh with large queries.** Confirmed. The panel
   polls approvals every 5 seconds and events, audit and traces every 10
   seconds (`webapp/src/store.jsx:66-76`), and those routes decrypt full rows
   with payloads (`src/aether/api/routes.py:233-279`, `292-315`, `844-875`,
   `878-898`). Two hours with the panel open cost about 100 MB in the replay.
6. **Full table reads on startup or wake.** Partly confirmed. Startup reads
   are bounded, not full-table: the newest `memory.hot_events` event rows
   (`src/aether/main.py:353`) and the newest `memory.hot_chunks` chunk rows
   (`main.py:363`), plus the schema audit
   (`src/aether/memory/storage.py:56-70`). They cost about 3 MB in the replay
   per restart. The real full read is the recall scan in item 1, not startup.
7. **`SELECT *` and unneeded sealed payloads.** Partly confirmed. The active
   SQL mostly names its columns, but recall fetches every chunk's text when
   only the top-k need it, and three proactive-store queries do use
   `SELECT *` (`src/aether/proactive/store.py:168`, `299`, `305`) — those run
   only when the background worker is enabled or its page is read.
8. **Frequent reconnects or connection churn.** Not confirmed in the code.
   There is one pool (`src/aether/memory/db.py:28-39`), every store reuses it,
   and no code creates a pool per request or per loop. Whether an idle
   connection alone keeps a Neon compute awake could not be verified from
   this repository; the provider's documentation is the authority, and the
   idle-connection test below is the plan.

## Every loop that touches the database

| Loop | Interval | What it does when nothing changed |
| --- | --- | --- |
| Agent tick (`loop.py:1073-1082`) | `agent.tick_seconds`, 30 s | Expiry update, two delivery reads, owner lookup (cached after first), stale-inbound read, observation read, reminder read, alert reads |
| Scheduler worker (`scheduler/jobs.py:240-247`) | 20 s | Reads every pending action with its sealed payload |
| Vector backfill (`loop.py:872-884`) | 1 s while catching up, 60 s caught up | One `indexed_at IS NULL` read |
| Provider runtime (`runtime.py:615-682`) | Every tick, network poll every 5 min | Reads connections and both cursors; writes three state rows when due |
| MCP source polls (`loop.py:2002-2059`) | `every_minutes`, default 5 min | Audits and ingests the empty result |
| Proactive path (`path.py:182-204`) | Every tick | Reads the whole open commitment set |
| Alert engine (`alerts/engine.py:178-184`) | Every tick | Reads pending notifications and the day's sent count |
| Proactive worker (`worker.py:209-226`) | `proactive.interval_minutes`, 30 min, off by default | One re-arm write at boot; disabled cycles do not query |
| Panel pollers (`store.jsx:66-76`) | 5 s and 10 s while a tab is open | Re-reads and decrypts every list |
| Telegram and Discord long polling | Platform-held connection | No database unless a message arrives; the processed-update ledger writes on arrival |

## What changes, in priority order

Phase 1 (shipped) is items 1-5, 7 and 8 below, with the numbers in
[After Phase 1](#after-phase-1). Phase 2's item 6 is shipped, with the
numbers in [After Phase 2](#after-phase-2). Phase 3's outage circuit is
shipped; its original write-behind queue was deliberately not built (see
item 7).

1. **Recall reads only what it ranks on, then only the text it cites.**
   Shipped twice: Phase 1 made the scan carry ids and sealed vectors and
   fetched the top-k text by id; Phase 2 replaced the scan with the resident
   matrix.
2. **Idle means no queries.** Shipped:
   - Provider poll cursors and state, the scheduler due time and the MCP
     poll schedule live in memory; the database is read once and written only
     when a value changes.
   - A poll that finds nothing performs zero queries — no audit row, no
     event, no cursor write.
   - The backfill loop runs at boot, on a failed index pass, and then once
     per `backfill_seconds`.
   - Expiry, delivery checks, stale-inbound checks and observation reads run
     in the background burst or on a dirty flag.
3. **Panel lists are pulled, not polled.** Shipped: no auto-refresh, a
   visible Refresh control, offset pagination capped by
   `panel_page_size`, and ETag conditional reads that return no body when
   unchanged.
4. **Batched scheduled work.** Shipped as `background_window_minutes`
   (default 15). Interactive messages never wait.
5. **One shared pool with configured size and idle release.** Shipped:
   `pool_min_size=0`, `pool_max_size`, and
   `max_inactive_connection_lifetime=pool_idle_seconds`.
6. **Memory-only retrieval caches.** Shipped: the vector matrix loads once
   in bounded batches and follows the chunk listener; ranking reads no
   `memory_chunks` rows. The lexical index and hot event view were already
   resident and are verified by the replay (no query sites). Entity lookups
   were left alone: the replay measured no entity queries in a day, and the
   loop already caches owner addresses per background burst.
   - The optional vector truncation in the plan is not shipped: it changes
     what is stored for a saving the matrix already provides, and it stays
     off unless a real quality evaluation asks for it.
7. **Outage circuit and graceful degradation.** Shipped: a circuit breaker
   with exponential backoff and jitter around the one pool, one plain notice
   when it opens and one when it closes, messages held in memory while it is
   open, and the recovery runbook below. The original bounded write-behind
   queue is deliberately not built: every advisory write it would have held
   (provider cursors, poll state, scheduler due times) is already memory-first
   and written only on change, so a queue would duplicate that state without
   making it safer. Approvals, authorization decisions and audit appends are
   never queued in either design — if the database cannot take them, they
   fail, which is the safe direction.
8. **Verification.** Shipped the before/after replay; the CI benchmark gate
   lands in Phase 4.

### Audit verification stays a full walk

The panel used to verify the whole audit chain on every poll. Removing the
pollers removes that cost. The verification itself is deliberately still a
full walk when the panel is opened or refreshed: an incremental check would
miss an edit to an old row while the process is running, and the chain's
tamper evidence is a hard rule.

## Configuration keys

All of these live under `db:` in `config.yaml`; see
[Configuration](configuration.md) for precedence and editing.

| Key | Default | Meaning |
| --- | --- | --- |
| `accounting` | `true` | measure every query for `aether db-stats` and the panel |
| `pool_min_size` / `pool_max_size` | `0` / `5` | the one shared connection pool |
| `pool_idle_seconds` | `60.0` | close an idle connection after this long |
| `breaker_failures` | `3` | consecutive connection failures that open the outage circuit |
| `breaker_base_seconds` / `breaker_max_seconds` | `2.0` / `300.0` | first probe wait, doubling with jitter to the max |
| `background_window_minutes` | `15.0` | non-urgent background burst cadence |
| `backfill_seconds` | `900.0` | fallback vector-backfill retry interval |
| `panel_page_size` | `100` | hard cap for one panel list page |
| `replay_messages_per_day` / `replay_mails_per_day` | `20` / `12` | replay shape |
| `replay_panel_hours` | `2.0` | how long the replay keeps a panel open |
| `daily_query_budget` | `2500` | replay ceiling and optional-work pause point, queries per UTC day |
| `daily_response_budget_mb` | `120.0` | replay ceiling and optional-work pause point, estimated MB per UTC day |
| `daily_active_minutes_budget` | `600` | replay regression ceiling, active compute per day |
| `monthly_compute_cu_hours` / `monthly_transfer_gb` | `100` / `5` | the provider's limits, used only for projection |
| `compute_cu` | `0.25` | the compute size used for the projection |
| `autosuspend_seconds` | `300` | the provider's idle window used for the active-time model |

The resident vector matrix has its own keys under `memory:`:

| Key | Default | Meaning |
| --- | --- | --- |
| `cache_batch` | `1000` | rows per boot-load query; a larger batch is fewer round trips |
| `vector_cache_max_chunks` | `8000` | most chunks kept resident; the oldest leave the cache, not the database |

## When the database is unavailable

The database is a metered, sometimes-absent dependency. A connection
failure is not a crash, and it is not answered with a retry storm.

**What the circuit does.** The shared pool counts consecutive connection
failures. After `db.breaker_failures` (3) it opens for
`db.breaker_base_seconds` (2), doubling on every failed probe with jitter up
to `db.breaker_max_seconds` (300). While it is open, every database call
raises `DatabaseUnavailable` immediately: no loop waits out a network
timeout and the process makes no queries. When the wait ends, one probe is
allowed. A success closes the circuit and logs
`database reachable again`; a failure reopens it for longer. A SQL error is
never counted — only connection refusals, dropped connections, a closed
pool and timeouts are.

**What still works.**

- The agent loop sends one plain sentence when the circuit opens and one
  when it closes. The notice opens no delivery row, so it can still reach a
  chat surface while the database is down; a transcript write is skipped by
  the failed circuit and costs nothing.
- Messages that arrive while the circuit is open stay queued in memory.
  The loop returns before draining them, so they are answered in order
  after recovery, not dropped and not invented.
- The panel and the web app keep serving pages that do not read the
  database. `/api/db/stats` reports the circuit's state, and the Database
  Use page shows it under Availability.

**What stops.** Turns, approvals, audit appends, memory reads and writes,
provider polls and the panel's database-backed lists fail while the circuit
is open. That is intentional: no approval is decided, no action runs and no
audit entry is invented without the database to record it. Nothing is held
in a queue that could later execute stale work.

**When it comes back.**

1. Confirm recovery: the Database Use page and `aether db-stats` show
   reachable, and the log has `database reachable again`.
2. Send a test message. Any held messages are answered on the next tick.
3. Do not restart the process to hurry it. A restart that cannot reach the
   database fails at boot by design, and a healthy process recovers on its
   own probe.

## What could not be verified

- Whether an idle connection by itself prevents a Neon compute from
  suspending. The pool release experiment in the plan settles it on the
  deployment itself.
- Exact provider billing. The byte figures are estimates of returned data,
  and the active-time model is a model. Only the provider's usage page is the
  meter.
- The production query mix. The replay mirrors the code's database calls but
  uses synthetic sizes; the first day of real `aether db-stats` output is what
  calibrates the synthetic day.
- The process's resident memory after the vector matrix loads. The Database
  Use page reports a computed estimate (chunks times dimensions times four
  bytes), not a reading of the process's RSS.
