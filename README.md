  # Aether

**A personal agent that actually acts — continuously, not just when asked.**
Aether watches your connected apps around the clock, keeps a memory of what
is happening across all of them, and takes real action on your behalf —
replying, booking, filing, following up — instead of just reminding you.
It runs whether or not you are talking to it: sources are polled on their
own schedules, events stream into memory as they happen, and a taught
routine fires the moment a matching event arrives. A human decision is
requested only when the action genuinely calls for one.

Aether is built around one premise — that a personal agent should **close
the loop itself**, executing the small, repetitive actions that fall out of
everyday digital life.

A person *can* keep track of everything happening across their email,
messaging apps, and calendar. What no one can realistically do is do it
**continuously, without fail, across every app, all day**. That's a hard
limit of attention and working memory, not discipline. Aether removes that
tax.

## Features

- **Continuous, unprompted monitoring** — connects to any app through MCP
  servers you configure (mail, calendar, whatever speaks MCP) plus native
  Telegram, Discord, and Slack connectors. Sources are polled on their own
  schedules; everything they report streams into memory as it happens. The
  agent works whether or not you are talking to it.
- **Executes real actions** — drafts and sends follow-ups, books, files,
  replies — not just reminders.
- **Routines** — teach a standing reaction once ("when mail from billing
  mentions an invoice, note it on my billing contact") and it fires whenever
  a matching event streams in. Trigger matching is deterministic — source,
  sender, keyword — with no LLM in the trigger decision, and every fire
  passes the authorization gate, so a taught routine that does something
  risky still asks you each time.
- **Memory that builds itself** — not notes you save: every ingested event
  lands in one deduplicated store (content-hash dedup, AES-256-GCM encrypted
  at rest), scored for salience so the agent attends to what matters, and
  resolved across platforms so the same person under different handles is
  one person in memory.
- **Deterministic authorization** — every action is classified by a
  pure-function policy *before* it runs: no LLM in the decision path, user
  rules in config, and unknown tools fail safe to approval. Low-stakes,
  reversible actions proceed automatically; higher-stakes ones queue for a
  one-tap decision. Every execution path — a chat proposal, a scheduled
  action firing hours later, a routine fire — passes the same gate.
- **Tamper-evident audit** — every decision produces a hash-chained record
  (SHA-256 over the previous entry), verifiable from the panel or on demand
  from any chat app: send `/verify` and the badge answers in plain words,
  with no model in the path — the same way `/config` answers about the
  configuration. Not an activity log — a chain: editing any
  past row breaks it visibly.
- **Decision replay** — every act — a chat turn, a routine fire, a
  scheduled action, an approved call — leaves an encrypted trace of what
  triggered it, what was proposed, how the gate ruled, and what came
  back. Ask "why did you do that?" and the answer comes from the record,
  not from a reconstruction. Reply "why?" to any Aether message in
  Telegram, Discord, or Slack and the trace answers in plain words —
  the record speaking, not the model.
- **Signal over noise** — a continuous event stream is filtered to a small
  set of genuinely relevant items, with no human ever labeling what matters.
- **Your contacts, your rules** — an explicit allowlist decides which
  contacts the agent may *see* on every channel; non-allowlisted senders
  are dropped at ingestion, their content never stored.
- **Screen-vision fallback** — for apps with no API, a companion CLI
  captures your screen on request; Aether reads and remembers what's on it.
  Strictly perception-only: it never acts inside an app it can't reliably
  interact with.
- **Restart-proof by construction** — scheduled actions are written to
  Postgres the moment they're created, never held only in memory, and
  overdue ones re-arm on boot. `kill -9` the process mid-flight; the action
  still fires — through the same gate.
- **Bring your own brain** — Claude, OpenAI, Gemini, or a local Ollama
  model; one line of config.
- **Configured from chat** — everything `config.yaml` holds is readable and
  changeable from Telegram, Discord, or Slack: send just `/config` and
  answer the questions — a step-by-step walk, no paths to memorize — or use
  the one-line commands as an expert. A change lands without a redeploy.
  Personal tuning (agent cadence, salience,
  the model itself) takes effect immediately — an llm change even restarts
  the process on its own — while the security sections (contacts, authz,
  messaging, MCP servers) hold for the same one-tap approval as any risky
  action. Changes persist encrypted in Postgres and override config.yaml
  until reset, so they survive restarts and ephemeral deploys. Keys never
  ride a /config line — they paste in `/apps` instead.
- **Apps connect from chat** — `/apps` lists what's connected and walks you
  through adding more. Known apps (Gmail, GitHub, Notion, Telegram, …) hand
  you the exact link and the numbered steps; keys paste right in the chat —
  consumed before ingest, stored encrypted, never echoed back — so no `.env`
  editing at all. OAuth apps are bring-your-own-client and per provider: the
  one-time Google setup collects your client ID and secret, and every Google
  app after is just a consent link sent in chat, which Aether receives at
  its own callback; a second Google app that needs more permissions asks for
  both in one re-approval, and the first app keeps working. Anything not in
  the catalog gets the same standardized questions. For the long tail
  there's a prebuilt hub: connect Composio once and 1000+ apps are each a
  single approve-click away, their actions picked up the moment you approve
  them. Every connection is verified in plain words — "connected ·
  24 actions" or "hasn't answered yet — I'll tell you the moment it's up" —
  and a server that comes up late, or grows new actions after you approve
  them, says so, unprompted. Config references keys by name only (`$NAME`),
  and OAuth tokens received from providers never pass through chat at all —
  both live encrypted like all other content.

## Architecture

```
┌───────────────────────── Aether (single always-on process) ─────────────┐
│                                                                         │
│  Chat surfaces ── Web UI (WebSocket) ─ Telegram ─ Discord ─ Slack      │
│        │                                                                │
│  Agent loop ── reason → act → observe, continuous                       │
│        │                                                                │
│  ┌─────┴──────────┬──────────────────┬───────────────────┐             │
│  LLM layer        Connector registry Memory store        Authz + audit  │
│  Claude/OpenAI/   native + MCP tools  encrypted,         deterministic  │
│  Gemini/Ollama    (flat namespace)    deduplicated,      policy, one-tap│
│  (configurable)                     entity-resolved     approvals,      │
│                                      (AES-256-GCM)      hash chain      │
│  Scheduler ── persisted scheduled actions + routines, survive restarts  │
└─────────────────────────────────────────────────────────────────────────┘
        │                                   │
   Neon Postgres                     companion CLI (grim → screenshot)
   (encrypted at rest)               screen-vision, perception-only
```

**One process, one loop.** A single always-on tick drains new events, scores
them for salience, fires any matching routines — deterministic matching, no
LLM in the trigger — and only then runs one reason → act → observe turn,
skipping the LLM entirely on a quiet tick. Whatever proposes a tool call — a
chat reply, a scheduled action firing hours later, a routine fire — it passes
the same authorization classifier before anything executes, and every
decision lands in the hash-chained audit log. Every act also leaves an
encrypted decision trace — the why behind the what — replayed on request
in chat, so "why did you do that?" is answered from the record.

**LLM providers** are user-configurable: Claude (Anthropic API), OpenAI,
Google Gemini, or local Ollama.

**Connectors**: anything that speaks [MCP](https://modelcontextprotocol.io)
connects through Aether's MCP host (user-configured in `config.yaml`).
Telegram, Discord, and Slack are built in natively because they are also the
chat/approval surfaces. WhatsApp is a documented stub — both free routes
(Meta test number, WAHA QR bridge) are written up in `docs/whatsapp.md`.

## Honest trade-offs (stated, not hidden)

- **Encryption at rest** uses AES-256-GCM with a server-held key. This
  protects the stored history against a database-level compromise — a leaked
  backup, a breached storage provider — and still lets the agent reason over
  the history while your devices are offline. It does **not** protect against
  a compromise of the server process itself; that is a separate, harder
  problem. Keys and OAuth tokens live in this same encrypted store — never
  in config.yaml, never in the audit record (which names keys only). The
  honest caveat on pasting: a key transits the chat platform and sits in
  its history until you delete the message — delete it right after the
  "stored." reply.
- **Free-tier hosting** (Render + Neon) is kept awake by a lightweight
  external uptime ping. Free Render services spin down after 15 idle minutes
  and are capped at 750 instance-hours/month; scheduled actions survive that
  because they are persisted the moment they're created and re-armed on boot.
- **LLM inference** happens at the configured provider. Zero-data-retention
  tiers and a dedicated secrets-management service are on the roadmap (a
  platform environment variable holds the encryption key today).
- **The prebuilt hub** (Composio, one optional recipe in `/apps`) puts a
  third party in the path of everything connected through it — the hub
  provider sees that app activity, and its free tier is metered per month.
  It's convenience, not a requirement: an app connected by its own recipe
  talks only to Aether and the app, never to anyone else's infrastructure.

## Quick start

```bash
cp .env.example .env          # fill in DATABASE_URL (Neon), AETHER_ENCRYPTION_KEY, AETHER_TOKEN
cp config.example.yaml config.yaml
pip install -e ".[dev]"
aether                        # serves on :8000 — /healthz, web chat at /
pytest                        # unit tests (no network, no DB)
```

Configuration is split cleanly:

| File | Holds |
|---|---|
| `.env` | secrets — database URL, encryption key, provider API keys, bot tokens |
| `config.yaml` | structure — LLM provider choice, MCP servers, messaging toggles, contact allowlist, authorization rules |

`config.yaml` is the boot base: anything changed from chat (`/config`) layers
on top of it, row by row in Postgres, until reset.

Next steps:

- **Deploy on free tiers** (Neon Postgres + Render, kept awake by an
  uptime ping): [DEPLOYMENT.md](DEPLOYMENT.md)
- **See it work** — nine scripted demos, from auto-actions and taught
  routines to kill -9 schedule survival, decision replay, the record
  proving itself from chat, the whole config changed from your pocket, and
  an app connected end-to-end without leaving it:
  [DEMO.md](DEMO.md)
- **WhatsApp**: documented stub, both free routes written up in
  [docs/whatsapp.md](docs/whatsapp.md)

## Security

If you believe you've found a security issue, please see
[SECURITY.md](SECURITY.md) before opening an issue.

## Contributing

PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). The test suite runs
with no network access and no database; integration tests are opt-in.

## License

[MIT](LICENSE) © Vedant-Git-dev
