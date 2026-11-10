---
title: "Sealed storage"
summary: "What is encrypted at rest, what may stay plaintext, and how to seal or rotate an existing history."
read_when:
  - You are reviewing what Aether stores
  - You are rotating the encryption key
  - You are running the sealer or the canary sweep
---

# Sealed storage

Everything Aether derives from the user's world is encrypted at rest. This page
covers what that means in the database, what may stay plaintext, and how to
seal an existing history.

## What is sealed

AES-256-GCM, a random nonce per value, and additional authenticated data that
binds each ciphertext to its table, column, row id and key version. Message
and mail text, subjects, addresses, names, filenames, media, captions,
summaries, facts, notes, watches, commitments, embeddings, salience and
urgency labels, timestamps of events, sources, delivery status text, tool
arguments and results, traces and error text that quotes content are all
sealed. The write-time enrichment labels also get the same treatment
(urgency, `needs_reply`, action type, deadlines and their original phrase),
the one-line summary, the linked entity ids, the focus object, the
conclusions cache and the outbound delivery body, message id and error.

Decrypted data exists only in process memory. It is never written to a temp
file, a log line, a metric label or the audit record. The audit record keeps
ids, counts and digests.

## What may stay plaintext

Only these classes of column are allowed outside the ciphertext:

- opaque ids and sequence numbers
- the content key version (`key_version`)
- keyed blind-index tokens (HMAC over an HKDF subkey, equality lookup only)
- status vocabulary (`pending`, `sent`, `active`, and the like)
- schema bookkeeping and internal timestamps (`created_at`, `indexed_at`,
  `last_used_at`)
- counters
- non-user configuration

No address, name, filename, label, source, event time, fact or body text may
be stored plaintext. The registry that encodes this rule lives in
`src/aether/memory/allowlist.py`; the boot audit reads it against
`information_schema.columns` and the CI guard reads it against the source.

Every encrypted value carries a self-describing envelope naming the key
version and purpose. A value written under one key version cannot be read as
another, and the same master key serves independent purposes without reusing
one key for two jobs. Values written by the older code (a bare nonce followed
by ciphertext) still decrypt; the sealer upgrades them.

## Sealing existing history

The expand migration (`017_sealed_storage.sql`) adds the sealed columns. The
cutover (`018_sealed_cutover.sql`) drops the plaintext originals. A deploy does
both in one boot: `run_migrations` invokes the sealer after `017` and before
`018`, so no manual step is required. If the sealer cannot seal a row the boot
stops and `018` never runs — a bad key cannot erase data. On a fresh database
the sealer finds nothing and the cutover applies immediately.

`018` also refuses to drop anything while a sealed copy is missing, so the
same guarantee holds if the SQL is ever applied by hand. The startup audit
default is now `enforce`: a column outside the registry stops the boot.

The CLI remains for a manual pass or a pre-deploy check. Run it with the same
`AETHER_ENCRYPTION_KEY` and key id the application uses.

```bash
# how much is left
python -m aether.memory.seal_migrate

# seal every table, 100 rows at a time, 50 ms between batches
python -m aether.memory.seal_migrate --apply

# one table only
python -m aether.memory.seal_migrate --apply --table events

# a resumable check; exits non-zero while anything is pending
python -m aether.memory.seal_migrate --verify-only
```

The sealer is idempotent and resumable: each job selects rows whose sealed
target is still null, so a crash or a failed row is retried on the next run.
A row whose plaintext cannot be decrypted is left untouched and counted as
failed; it is never replaced with a guess. Nothing is printed but table names
and counts.

## What is not guaranteed

The ciphertext protects a leaked database dump, a stolen backup or a breached
storage provider. It does not protect the running process: whoever can read
`AETHER_ENCRYPTION_KEY` and query the database can read the same rows the app
does.

Historical copies can outlive the cutover. A database backup, a WAL segment, a
provider snapshot or a Neon point-in-time restore taken before `018` may keep
the old plaintext columns until that retention window expires. Re-encrypting
the live tables does not rewrite those copies. For a clean boundary, take a
fresh database after the cutover and retire the old restore points; there is
no way to reach into an existing provider snapshot and rewrite it.

## Workspace files

The five curated Markdown files plus daily notes are one sealed row each in
`workspace_files`. The display name is sealed too; only a keyed blind token
(the name lookup) and the row id stay plaintext. A missing key or a rotated-away
row reads as empty and never as text. On the first boot after the upgrade, a
file-backed workspace is imported row by row, and each file is deleted only
after its row commits.

The panel editor reads `/api/workspace` and saves through
`PUT /api/workspace/{file}`; decryption happens on the server and in the
browser tab, never in a temp file. `POST /api/workspace/export` returns a
scrypt+AES-256-GCM bundle sealed under a passphrase you choose;
`POST /api/workspace/import` opens one. A wrong passphrase and a corrupt
bundle read the same: one plain refusal.

## The in-memory index

Because event time, source, kind and salience live inside `meta_enc`, Postgres
cannot sort or filter them. The agent keeps a decrypted hot set in process
memory: events by sequence, by time and by salience, rebuilt at boot from the
newest `memory.hot_events` rows and kept current by every ingest and salience
write. A query that needs an older window cold-scans `memory.cold_batch` rows
at a time and folds them into the same index; it never scans the whole table
for one question. Nothing in the index is persisted.

The index also carries the write-time labels decoded out of `meta_enc`
(urgency, `needs_reply`, deadlines, summary, linked entity ids) and a
`by_entity` map, so the attention view, the entity timeline and the commitments
view are lookups over the hot set rather than SQL sorts over encrypted
columns. Exactly one plaintext column was added for this: `memory_chunks.ordinal`:
a chunk's position inside its own event. Retrieval needs the chunk before and
after a hit, and row ids interleave across events under concurrent ingest, so
id order is not document order. The ordinal leaks order and chunk count, never
content.

## Structured stores

- `facts` — durable claims the user stated or an event carried. The subject is
  a plaintext identity id (or null), the (subject, key) lookup is a keyed blind
  token, and the status word (`active`, `superseded`, `disputed`, `forgotten`)
  is plaintext vocabulary. The key, value, valid window, provenance event ids,
  confidence and trust flag are sealed. A superseded claim keeps its words and
  gains an end of validity; a forgotten one keeps its history but never reads
  back; an untrusted claim on identity, contacts, policy or watches is held
  disputed and asked about once.
- `forgotten_events` — the tombstone that makes forgetting stick. One row per
  forgotten event, holding the same keyed `dedup_mac` and `source_token` the
  event carried and nothing else: no content, no plaintext. `ingest` refuses a
  message whose mac is here, so the next poll cannot store it again.
- `summaries` — one sealed row per (kind, period): a thread, a day, or a week.
  The kind and the keyed period token are plaintext; the text and its source
  event ids are sealed.
- `commitments` — what the user owes and is owed. Kind, status, counterparty
  and source event are plaintext vocabulary and ids; the text, due time,
  original phrase, urgency and the artifact links (thread, document name,
  media id) are sealed.
- `watches` — the standing instructions the user asked for. The scope is a
  keyed blind token, the channel and status are plaintext vocabulary, and the
  id watermark and timestamps are bookkeeping; the label and every criterion
  are sealed and bound to the row.
- `commitment_reminders` — which reminder lead already fired for which
  commitment. Ids, integer minutes and a timestamp only.
- `focus` — one working set per chat, keyed by a blind token. The recent
  entities, last offer, open task and last attachment are sealed.
- `conclusions` — live-state answers keyed by a blind token, with `as_of`,
  `valid_until` and source sealed alongside the value. An entry past
  `valid_until` is never served.
- `deliveries` — one row per outbound send attempt. Status is plaintext;
  the body, platform message id and error are sealed. Errors are hashed
  before they reach a log line.
- `inbound_messages` — one terminal outcome per message the user sent.
  The surface and status word are plaintext; the chat, message and
  idempotency keys are blind tokens; the failure reason and the outbound
  message id are sealed. It is what stops an old question being answered
  again on a later unprompted tick.

## Logs and canaries

Application code logs ids and counts, never content: no handle, label, subject,
filename, address or error text that could quote one. A scrubber is installed on
the root handlers and on the access log as a safety net, rewriting tokens in
URLs, contact links, addresses, home paths, filenames and long quoted snippets
before any handler sees the record. `tests/test_logging_setup.py` pins that
behaviour.

`tests/test_canary.py` is the end-to-end check. One unique string is pushed
through every write path — event text and subject, sender address, note,
approval, schedule, routine, trace, proactive task, media filename, chat line,
sync cursor and audit outcome — and then every column of every table is swept
for it. Outside the migration window the sweep must be empty. While the legacy
plaintext columns still exist, the canary may appear only in the columns listed
in `LEGACY_PLAINTEXT`; the cutover removes those. The captured log text must
never contain it, and the sealed payload must still decrypt to contain it.

## Key rotation

Set the new key as `AETHER_ENCRYPTION_KEY` with a new `AETHER_ENCRYPTION_KEY_ID`,
and move the old value to `AETHER_ENCRYPTION_KEY_PREVIOUS` with its id. Old
sealed values stay readable; new writes use the new version.

Blind-index tokens and dedup tags are derived from the active key, so lookups
stop matching rows written under the previous key until they are rewritten.
Run the re-tokenize pass while both keys are configured:

```bash
python -m aether.memory.seal_migrate --retokenize
```

It rewrites the derived token columns for workspace files, events (including
their dedup tags), handles, chat surfaces, chunk model filters, proactive
tasks, and media origins and content tags, and exits non-zero if any row could
not be re-derived. The pass re-tokenizes
indexes, not ciphertext: sealed blobs written under the previous key keep
opening only while that key stays configured, so keep it set until a
re-encryption pass exists. Chunks sealed before the model name was recorded in
their sealed metadata cannot be re-tokenized; they are counted as failures and
should be re-embedded.