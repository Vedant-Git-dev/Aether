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
sealed.

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

The expand migration (`017_sealed_storage.sql`) adds the sealed columns and
leaves the plaintext in place. The sealer fills them, verifies each round trip,
and reports what remains. The cutover migration that drops the plaintext
columns ships with the stores that read the sealed copies.

Run it with the same `AETHER_ENCRYPTION_KEY` and key id the application uses.

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

Take a database backup before `--apply`. The plan's own caveat applies: a
backup, WAL segment or provider snapshot may keep an old plaintext copy until
it expires. The fresh-database cutover path exists for deployments that need a
clean boundary.

## The in-memory index

Because event time, source, kind and salience live inside `meta_enc`, Postgres
cannot sort or filter them. The agent keeps a decrypted hot set in process
memory: events by sequence, by time and by salience, rebuilt at boot from the
newest `memory.hot_events` rows and kept current by every ingest and salience
write. A query that needs an older window cold-scans `memory.cold_batch` rows
at a time and folds them into the same index; it never scans the whole table
for one question. Nothing in the index is persisted.

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
values stay readable; new writes use the new version. Blind-index tokens are
derived from the active key, so a rotation also needs a re-tokenize pass before
lookups match rows written under the previous key.