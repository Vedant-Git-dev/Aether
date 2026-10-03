# Deploying Aether

The reference deployment runs on two free tiers — **Neon** (Postgres) and
**Render** (web service) — and stays awake with a lightweight external
uptime ping. Everything is one always-on process: the API, the web chat,
the agent loop, the scheduler, and the connectors.

> Running locally instead? `cp .env.example .env`, fill it in,
> `pip install -e ".[dev]"`, `aether`. Everything below only matters for
> the hosted deployment.

## What you need

- A GitHub repo with this code
- A [Neon](https://neon.tech) account (free tier)
- A [Render](https://render.com) account (free tier)
- An uptime pinger account ([UptimeRobot](https://uptimerobot.com) or
  [cron-job.org](https://cron-job.org) — both free)

## 1. The database (Neon)

1. Create a Neon project. In the dashboard, copy the **connection string**
   — it looks like `postgresql://user:pass@host/dbname?sslmode=require`.
   Keep the `sslmode=require` part.
2. That's it. Aether runs its own migrations on boot
   (`schema_migrations` tracks them); there is nothing to apply by hand.

## 2. Secrets

Generate an encryption key locally — this is the AES-256-GCM key that
encrypts every memory, note, and message at rest:

```bash
python -c "from aether.memory.crypto import generate_key_b64; print(generate_key_b64())"
```

You'll fill three values in Render's dashboard in the next step:

| Value | What |
|---|---|
| `DATABASE_URL` | the Neon connection string |
| `AETHER_ENCRYPTION_KEY` | the base64 key you just generated |
| `AETHER_TOKEN` | any long random string — it guards the API, the web chat, and companion uploads |

Plus the API key for whichever LLM provider you picked in `config.yaml`
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `GEMINI_API_KEY`), and any
messaging bot tokens you want.

**Losing the encryption key means losing the memories** — the data is
unrecoverable without it. Save it somewhere that isn't the Render
dashboard alone.

## 3. The web service (Render)

The repo ships a Blueprint (`render.yaml`), so the shortest path is:

1. Render dashboard → **New → Blueprint**, pick your repo.
2. Render reads `render.yaml`, asks for the env vars above, and deploys.
   (Manual route works identically: New → Web Service, Python runtime,
   build `pip install .`, start `aether`, free plan.)
3. Watch the deploy logs. First boot should end with
   `aether running — surfaces: web…` and a `database ready` line.
4. Visit `https://<your-app>.onrender.com/healthz` — `{"ok": true}`.

Set `AETHER_TOKEN` in the web UI (top right) and you have web chat,
the approvals panel, the audit view, and the events feed — the whole
panel.

**config.yaml on the server**: structure (MCP servers, messaging
toggles, allowlist, authz rules) is read from `config.yaml` at the
repo root. It holds no secrets, so commit yours before deploying —
otherwise the hosted instance runs on defaults (web chat only, no
external connectors). `config.yaml` is gitignored by default to keep
*your* local copy out of the repo; `git add -f config.yaml` when you
want the deployed instance to use it.

## 4. Keeping it awake (the part free-tier guides skip)

Render free services **spin down after 15 idle minutes** and are capped
at **750 instance-hours/month** — just about enough for one service
running 24/7. The fix for the first is an external pinger:

1. Create a monitor (UptimeRobot) or cron job (cron-job.org) for
   `https://<your-app>.onrender.com/healthz` every **10 minutes**.
2. That's the whole trick — `/healthz` is a plain 200 with no database
   or LLM work, so it costs nothing but keeps the instance resident.

Even when the instance does restart or spin down, nothing is lost:
**scheduled actions are persisted the moment they're created** and
re-armed on boot, the MCP polls catch up on their next tick, and chat
history lives in Postgres.

## 5. Connecting your apps

Most of this moved into chat: send `/apps` and the walk hands you the
exact link and the numbered steps for each app, and keys paste right into
the conversation — consumed before the agent ever sees them (no LLM, no
memory, no trace), stored encrypted, never echoed back. The dashboard env
vars and `.env` still work for every key — the walk checks for them
first, so nothing needs a redeploy to add an app.

**The one real setup left is Google**, because its apps (gmail, calendar)
sign in with OAuth through a client **you** own — bring your own client,
once per provider, not once per app:

1. `https://console.cloud.google.com/apis/library` — in one project,
   enable **Gmail API**, **Calendar API**, and each matching **"MCP API"**.
2. `https://console.cloud.google.com/apis/credentials/consent` — create
   an **External** consent screen and **publish it to Production**.
3. `https://console.cloud.google.com/apis/credentials` — create an OAuth
   client (**Web application**) with redirect URI
   `https://<your-app>.onrender.com/oauth/callback`
   (locally: whatever public URL you're reachable at — set
   `AETHER_PUBLIC_URL` and the walk stops asking for it).

Paste the client ID and secret into the walk when it asks; every Google
app after is just a consent link it sends you — and a second one that
needs more permissions asks for both sets in one re-approval, so the
first keeps working.

Honest caveats, because it's your own client, not a verified one:

- Google shows an **"unverified app"** warning on first sign-in —
  expected. Click through it once (Advanced → proceed). Verification is
  for apps serving the public; yours serves you.
- In **Testing** status, refresh tokens die after **7 days** and only
  100 users can ever grant — step 2's publish to Production fixes both.
- **Gmail's scope is "restricted"**, which for a public app means an
  annual security assessment. For a personal deploy the unverified
  click-through is the whole cost. That's the BYOA trade: no central
  gateway sees your mail, because there is no central gateway.

The manual paths, for the record:

- **MCP servers** (`config.yaml → mcp_servers`): anything with an HTTP
  transport works from anywhere — the remote servers Google publishes
  for Calendar/Gmail are the easiest start. `stdio` servers need their
  binary present on the host, which the Render Python runtime generally
  won't have (no Node for `npx …`, for example) — run stdio servers on
  your own machine and expose them over HTTP, or stick to remote ones on
  the hosted instance. Locally, stdio servers work as configured.
- **Telegram / Discord / Slack**: paste the bot token into `/apps` and
  the walk flips the toggle itself (or put the tokens in the dashboard
  env vars and flip the matching `enabled: true` in `config.yaml`).
  Telegram uses long polling and Slack uses Socket Mode, so neither
  needs a public webhook URL.
- **The companion** (`companion/aether_snap.py`) runs on **your machine**,
  not on Render — it needs your screen. It has its own token flag:
  `python companion/aether_snap.py --url https://<your-app>.onrender.com \
  --token <AETHER_TOKEN> --poll`.
- **Ollama** runs on your machine only — there is no free tier that can
  host it, so an Ollama-backed Aether is a local run, not a Render one.

## 6. Verifying the deploy

- `/healthz` → `{"ok": true}`
- Web chat: send "what do you remember?" — a reasoned reply means the
  provider config is right.
- Propose something risky in chat ("reply to that email") → an approval
  card appears on the panel and on your chat surfaces; the audit panel
  shows the `approve` row and the chain badge stays green.
- Schedule something for two minutes out, restart the service from the
  dashboard, watch it fire after boot — the re-arm line is in the logs.

## Costs, honestly

| Thing | Free tier | Watch out for |
|---|---|---|
| Neon Postgres | 0.5 GB storage | plenty for text memories |
| Render web service | 750 instance-hrs/month | 15-min spin-down without the ping |
| UptimeRobot / cron-job.org | 5-min intervals free | 10-minute ping is fine |
| LLM provider | pay-per-token | a quiet day with nothing new costs zero tokens — quiet ticks make no LLM call |

If any of that becomes uncomfortable, the same process runs anywhere that
hosts a Python web service — only the Render specifics above change.
