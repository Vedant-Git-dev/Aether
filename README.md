# Aether

**A personal agent that actually acts.** Aether continuously watches your
connected apps, remembers what's happening across all of them, and takes real
action on your behalf — replying, booking, filing, following up — instead of
just reminding you. It asks for a human decision only when the action
genuinely calls for one.

Most personal-productivity tools stop at organizing or surfacing information:
a dashboard, a summary, a notification. Aether is built around a different
premise — that a personal agent should **close the loop itself**, executing
the small, repetitive actions that fall out of everyday digital life.

A person *can* keep track of everything happening across their email,
messaging apps, and calendar. What no one can realistically do is do it
**continuously, without fail, across every app, all day**. That's a hard limit
of attention and working memory, not discipline. Aether removes that tax.

## Features

- **Continuous cross-app monitoring** — connects to any app through MCP
  servers you configure (mail, calendar, whatever speaks MCP) plus native
  Telegram, Discord, and Slack connectors, and keeps a running, deduplicated
  memory instead of a raw activity log.
- **Cross-platform identity resolution** — the same person appearing under
  different handles on different platforms is linked into one coherent memory
  of that relationship.
- **Executes real actions** — drafts and sends follow-ups, books, files,
  replies — not just reminders.
- **Routines** — teach a standing reaction once ("when mail from billing
  mentions an invoice, note it on my billing contact") and it fires whenever a
  matching event streams in. Trigger matching is deterministic — source,
  sender, keyword — with no LLM in the trigger decision, and every fire
  passes the authorization gate, so a taught routine that does something
  risky still asks you each time.
- **Screen-vision fallback** — for apps with no API, a companion CLI captures
  your screen on request; Aether reads and remembers what's on it. Strictly
  perception-only: it never acts inside an app it can't reliably interact
  with.
- **Deterministic authorization + audit** — every action is classified against
  a declared policy *before* it runs. Low-stakes, reversible actions proceed
  automatically; higher-stakes ones queue for a one-tap decision. Every
  decision produces a tamper-evident, hash-chained audit record.
- **Signal over noise** — a continuous event stream is filtered to a small
  set of genuinely relevant items, with no human ever labeling what matters.
- **Your contacts, your rules** — an explicit allowlist decides which contacts
  the agent may see on every channel; non-allowlisted senders are dropped at
  ingestion, their content never stored.
- **Bring your own brain** — Claude, OpenAI, Gemini, or a local Ollama model;
  one line of config.

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
  problem.
- **Free-tier hosting** (Render + Neon) is kept awake by a lightweight
  external uptime ping. Free Render services spin down after 15 idle minutes
  and are capped at 750 instance-hours/month; scheduled actions survive that
  because they are persisted the moment they're created and re-armed on boot.
- **LLM inference** happens at the configured provider. Zero-data-retention
  tiers and a dedicated secrets-management service are on the roadmap (a
  platform environment variable holds the encryption key today).

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

Next steps:

- **Deploy on free tiers** (Neon Postgres + Render, kept awake by an
  uptime ping): [DEPLOYMENT.md](DEPLOYMENT.md)
- **See it work** — five scripted demos, from auto-actions and taught
  routines to kill -9 schedule survival: [DEMO.md](DEMO.md)
- **WhatsApp**: documented stub, both free routes written up in
  [docs/whatsapp.md](docs/whatsapp.md)

## Security

If you believe you've found a security issue, please see
[SECURITY.md](SECURITY.md) before opening an issue.

## Contributing

PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). The test suite runs
with no network access and no database; integration tests are opt-in.

## License

[MIT](LICENSE) © Aether contributors
