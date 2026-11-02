# Troubleshooting

Start with the startup log and `/status`. Both are plain language and neither costs a
model call.

## Boot stops immediately

### `DATABASE_URL is not set`

Aether persists everything to Postgres; there is no in-memory or SQLite mode. Create a
database and put its URL in `.env`:

```dotenv
DATABASE_URL=postgresql://user:pass@host/dbname?sslmode=require
```

For a local database:

```bash
createdb aether
DATABASE_URL=postgresql://localhost/aether
```

### `AETHER_ENCRYPTION_KEY is not set`

Generate one:

```bash
python -c "from aether.memory.crypto import generate_key_b64; print(generate_key_b64())"
```

Put the output in `.env`. For a throwaway dev instance only, you can set
`AETHER_DEV_EPHEMERAL_KEY=1`; the key changes every boot, so encrypted data from the
previous boot is unreadable.

### `AETHER_ENCRYPTION_KEY is invalid`

The value is not valid base64 for 32 bytes. Generate a fresh one. Do not try to repair a
damaged key if it protects data you need; a fresh key cannot read the old rows.

### `bad authz rule`

A `tool_pattern` or `param_pattern` is not a valid Python regular expression. The error
names the rule. Fix it in `config.yaml`, or reset the override:

```text
/config reset authz.rules
```

### `provider token keyring`

`AETHER_TOKEN_ENCRYPTION_KEY` is malformed, or the previous-key pair is inconsistent.
Both keys must be base64 for 32 bytes. Unset the keyring to boot without direct provider
connections.

### `address already in use`

Another process owns port 8000. Stop it, or start Aether on another port:

```bash
PORT=8010 aether
```

## The panel shows 503 "panel not built"

The server is running, but the Vite build is missing:

```bash
cd webapp
npm ci
npm run build
cd ..
```

The output goes to `src/aether/web/`, which is gitignored. Restart is not required for
the files themselves, but the server only mounts the panel when `index.html` exists at
startup, so restart `aether` after the first build.

## The panel loads but every page is empty or 401

The browser has no token, or the wrong one.

1. Open **Settings > Access**.
2. Paste the value of `AETHER_TOKEN`.
3. Save.

The token is stored in this browser's local storage. Clearing site data removes it.

## Chat replies say the model is unavailable

The process booted, but no LLM provider was built. Check:

- `llm.provider` in `config.yaml` matches the key you set.
- The key is in `.env`, or pasted under **Settings > Credentials**.
- The model id is one that provider serves.
- For Ollama, the server is running at `OLLAMA_BASE_URL`.

The log line at boot names the provider failure. The panel, audit, approvals, and
settings keep working while the model is down.

## `/status` shows "nothing set" for a medium

An empty media slot means that medium is off. Set it to `default` to use the main model,
or to a named vision-capable model:

```text
/config set llm.image_model default
```

`llm.*` changes restart Aether in place, so wait a few seconds.

## Memory search is unavailable

Semantic recall needs `GEMINI_API_KEY` and a Gemini embedding model. The default is
`gemini-embedding-001`. A value pointing at another provider is refused with a warning
rather than guessed.

Without embeddings, recent events and keyword search still work. Only meaning-based
recall is off.

## Telegram does not send anything

- The bot token is set in `.env` and `messaging.telegram.enabled` is `true`.
- You have sent the bot at least one direct message since the process started. The
  connector learns its reply chat from the first inbound message.
- No other process is polling the same bot token. Telegram allows one `getUpdates`
  consumer; a second instance logs the conflict.

Approvals are still visible in the web panel while Telegram is unavailable.

## Discord ignores messages

- The bot token is set and `messaging.discord.enabled` is `true`.
- **Message Content** is enabled under the application's privileged intents.
- You are sending a direct message, not a guild channel message.
- You have sent the bot at least one DM since startup.

## An MCP server did not connect

A broken MCP server must never take Aether down, so it logs and the rest boots.

- For stdio, the command exists and is on `PATH`.
- For HTTP, the URL is reachable from the server, not just your laptop.
- Headers reference secrets as `$NAME`, and the secret exists in `.env` or the encrypted
  store.
- The server answers a tool listing. Some servers need a first-run login.

`/apps` shows what is connected. The Apps page shows the same.

## Every action is asking for approval

That is the fail-safe working. Check the Audit page: the `rules_matched` field names the
rule or built-in that parked the call.

- `default:fail-safe` means the tool name matched no rule and no verb. Add a user rule
  for the exact name if you trust it.
- `builtin:risky` means the name contains a verb such as `send`, `create`, or `delete`.
- `builtin:provider-write` means the provider declared the tool as a write.

Rules are evaluated first, so a user rule can loosen the built-ins for the tools you
trust. See [policy.md](policy.md).

## Something was approved but never ran

- The approval may have expired. Its row is marked `expired` and audited.
- The tool may have been removed since the request was parked, for example after
  disconnecting an app. The panel says it could not run and the row is marked failed.
- For a background task, the skill revalidates live state before firing. If the source
  event moved on, the draft is dropped and the task is marked stale.

## The background worker stopped on its own

The anomaly monitor pauses it after three refused runs from one skill, or three discards
in a row. This is deliberate and it survives restarts. Start it again:

```text
/resume
```

The pause reason is in the audit log and in the notification Aether sent.

## `/verify` reports a broken chain

A failed chain means stored audit rows no longer match their own hashes. Possible causes
include a restored partial backup or manual database edits. Aether reports the first
broken sequence number and does not rewrite history. Treat the decision record from that
point on as unreliable.

## Tests skip

- Integration tests skip unless `AETHER_TEST_DATABASE_URL` is set. They truncate every
  table they touch, so never point them at a real database.
- E2E tests skip unless `AETHER_TEST_E2E=1` is set and Chromium is installed:

  ```bash
  playwright install chromium
  AETHER_TEST_E2E=1 python -m pytest tests/e2e -q
  ```

## Resetting

Reset one setting to its YAML or default value:

```text
/config reset agent.tick_seconds
```

Reset everything changed from chat or the panel: review overrides with `/config show`,
then reset them one path at a time. Aether does not have a single "factory reset" because
that would also discard policy and allowlist decisions you made deliberately.

To start over completely, use a fresh database and a fresh encryption key. The old
database remains readable only with the old key; delete it yourself when you are sure.
