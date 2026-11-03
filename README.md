# Aether

**A personal agent you can give write access to, because it shows its work.**

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/Vedant-Git-dev/Aether/actions/workflows/ci.yml/badge.svg)](https://github.com/Vedant-Git-dev/Aether/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)

Aether is a self-hosted personal agent that reads your mail, calendar, and chat,
remembers what is happening across them, and takes actions on your behalf. Before
anything runs, deterministic code classifies it against a policy you wrote:
low-stakes reads go ahead, and anything that sends, changes, or deletes waits for one
tap in chat. Every authorization decision is appended to a hash-chained audit log you
can verify with `/verify`.

## What is Aether

Aether is built for the part most assistants skip: letting the agent act without asking
you to trust it blindly. The model proposes; code decides; the record proves what
happened.

- **Policy decides, not the model.** Regular expressions on tool names and call
  parameters, evaluated before execution. No LLM in the decision path.
- **One-tap approvals.** Higher-stakes calls park in the web panel, Telegram, or Discord
  with the exact action and its parameters.
- **Tamper-evident audit.** Each record commits to every record before it with SHA-256.
  `/verify` re-walks the chain and reports the first break.
- **Cross-app memory.** Events are deduplicated, people are resolved across handles, and
  recall is semantic when the free embedding model is configured.
- **Bring your own model.** Anthropic, OpenAI, Google Gemini, NVIDIA NIM, or a local
  Ollama model, with separate slots for images, audio, and video.

## A day with Aether

On Monday you tell a recruiter you will send your availability by Thursday. Thursday is
busy, and the thread goes quiet.

**Without Aether:** the promise stays in your head until it is too late.

**With Aether**, with the background worker on, the follow-up skill finds the promise in
memory (a memorable event with a marker such as "I will" or "follow up"), drafts from its
words, and proposes `gmail__send_email`. The policy parks the outbound send, an approval
card arrives with the recipient, subject, and body, and you tap Approve. The skill re-checks
that the source event still exists before firing; if the thread moved on, the draft is
dropped and you are told why. The audit row records the rule, the decision, and who
approved it.

With the worker off, the same thing happens when you ask in chat. The gate is identical.

## Demo

```text
You       Reply to Sam's email and confirm Friday at 10:00.

Aether    I can send that, but it leaves Aether. Approve it first.
          [ Approve ]   [ Deny ]   [ Read the full text ]

You       Approve

Aether    Sent. The decision record shows the rule that matched and your approval.
```

The same decision appears on the Audit page with a `chain ok` badge and in the expanded
Decision Trace with every tool call and its ruling.

## How an action flows

```mermaid
flowchart TD
    A["Event, schedule, or chat request"] --> B["Agent loop<br/>reason, act, observe"]
    B --> C{"Deterministic policy<br/>code, not the model"}
    C -->|"Read-only, internal, or allowed by your rule"| D["Execute"]
    C -->|"Sends, changes, deletes, or unknown"| E["Approval card<br/>web, Telegram, Discord"]
    E -->|"Approve"| D
    E -->|"Deny or expire"| F["Not run"]
    D --> G["Tool or connected app"]
    G --> H["Hash-chained audit record"]
    F --> H
```

1. **Trigger.** A polled event, a scheduled action, a routine, a background task, or a
   message from you.
2. **Reasoning.** The agent loop uses your configured model, recent context, and the
   tools currently connected.
3. **Classification.** `authz/policy.py` evaluates, in order: your rules, config-write
   gates, provider read/write effects, internal tools, web research, image generation,
   risky verbs, read-only verbs, and the fail-safe.
4. **Gate.** Allowed calls execute. Provider writes and anything unknown park. An
   unknown tool can never run silently.
5. **Execution.** The call runs through the connector registry or an MCP server.
6. **Record.** The audit log appends the decision, matched rule, parameter digest, and
   outcome. Parameter values are never stored.

## Features

### Trust and control

- **Deterministic authorization.** First matching rule wins; no model in the path. See [the policy guide](docs/policy.md).
- **One-tap approvals.** Approve or deny from the panel, Telegram, or Discord; requests expire after 24 hours by default.
- **Tamper-evident audit.** SHA-256 chain over every decision, verified with `/verify`.
- **Contact allowlists.** A cross-app list plus per-app lists for Gmail, Telegram, and Discord; non-allowlisted senders are dropped at ingestion.
- **Security changes park.** Contacts, authz, messaging, and MCP changes always wait for approval, even from chat.

### Memory and understanding

- **Deduplicated event memory.** A repeated poll result is stored once.
- **Cross-platform identity.** The same person under different handles is merged into one entity.
- **Salience scoring.** A first-pass model, or the main model, decides what is worth mentioning; it can be turned off independently.
- **A workspace you confirm.** Markdown identity, tone, preferences, and durable memory; inferred profile entries wait until you say yes.
- **Semantic recall.** Gemini embeddings make memory searchable by meaning.
- **Files become memory.** Images, PDFs, documents, audio, and video frames are described or extracted into encrypted events.

### Reach

- **Native app providers.** Gmail, Calendar, Drive, Docs, GitHub, and LinkedIn, with read and write tools and polls where they make sense.
- **MCP.** Any stdio or HTTP MCP server, including scheduled poll tools.
- **Chat surfaces.** The web panel, Telegram, and Discord, all carrying approvals. See [channels](docs/channels.md).
- **Screen vision.** A Wayland companion captures your screen on request; Aether reads and remembers it. Perception only, never action.
- **Background work.** Follow-ups, web research, and media jobs inside daily budgets, with results that wait for a verdict. See [the workbench](docs/workbench.md).
- **Media.** Generate images, edit video with a fixed operation set, and transcribe voice notes.

### Flexibility

- **Model per role.** Route images, audio, video, the first pass, and embeddings separately.
- **Three ways to configure.** `config.yaml`, the Settings panel, or `/config` in chat; chat changes persist and reset to the YAML baseline.
- **Self-host anywhere.** One asyncio process and Postgres. A Render blueprint is included.
- **Persistent schedules.** Scheduled actions and routines survive restarts and re-arm on boot.

## Quick start

Aether needs Postgres to boot. There is no hosted instance and no SQLite mode. To click
through the panel first, use the fixture server, which needs no database. The long version
is in [docs/quickstart.md](docs/quickstart.md).

### Try the panel without a database

Real app, real panel, in-memory fixtures. It does not run the agent, call a model, or
persist anything.

```bash
git clone https://github.com/Vedant-Git-dev/Aether.git
cd Aether
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cd webapp && npm ci && npm run build && cd ..
python tests/e2e/dev_server.py
```

Expected: `Uvicorn running on http://127.0.0.1:8731`. Open it and paste the token
`secret` under **Settings > Access**. The data is fake and nothing is saved.

### Self-host

You need Python 3.12+, Node 22+ to build the panel, and a Postgres database. A free Neon
database works; so does a local one.

**1. Install and create the database**

```bash
git clone https://github.com/Vedant-Git-dev/Aether.git
cd Aether
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
createdb aether          # or use a Neon connection string
```

**2. Generate a key and write `.env`**

```bash
python -c "from aether.memory.crypto import generate_key_b64; print(generate_key_b64())"
cp .env.example .env
```

Fill in the required values:

```dotenv
DATABASE_URL=postgresql://user:pass@host/dbname?sslmode=require
AETHER_ENCRYPTION_KEY=<the base64 line from the command above>
AETHER_TOKEN=<a long random string>
ANTHROPIC_API_KEY=<your provider key, or use OpenAI, Gemini, or NVIDIA>
```

Save the key. Losing it means losing every stored message, memory, and token.
`AETHER_TOKEN` is a shared secret, not a user account.

**3. Write `config.yaml` and build the panel**

```bash
cp config.example.yaml config.yaml
cd webapp && npm ci && npm run build && cd ..
```

The example selects Anthropic and `claude-opus-5`. Change `llm.provider` and `llm.model`
if needed. Without the build, `/` returns a plain 503.

**4. Run and check**

```bash
aether
# in another terminal
curl http://localhost:8000/healthz
```

Expected:

```json
{"ok":true}
```

Open `http://localhost:8000`, paste `AETHER_TOKEN` under **Settings > Access**, and send
`/status`. It reports the resolved models, pending approvals, and queue depth without a
model call. Then ask a normal question; a reply means the provider key works. If chat says
the model is unavailable, the key is missing or wrong. The panel, audit, and settings
still work. If boot fails, see [troubleshooting](docs/troubleshooting.md).

### First connectors

Connect from the Apps page or the chat walk:

```text
/apps add gmail
```

A pasted value is consumed before the model sees it, stored encrypted, and never echoed
back. MCP servers can be declared in `config.yaml`; see
[docs/configuration.md](docs/configuration.md).

Optional services:

`TAVILY_API_KEY` enables web search (without it, `web_search` refuses and `web_fetch`
still works). `GROQ_API_KEY` enables voice transcription. Modal, Vercel, or a GitHub
Actions batch queue enable background jobs. `GEMINI_API_KEY` with a Gemini embedding
model enables semantic memory. Screen capture needs Wayland, `grim`, and an image model.

## Configuration

Secrets live in `.env`. Structure and policy live in `config.yaml`. Settings changed from
chat or the panel are stored in Postgres and override the YAML until reset.

| File | Holds | Examples |
| --- | --- | --- |
| `.env` | Secrets | `DATABASE_URL`, `AETHER_ENCRYPTION_KEY`, `AETHER_TOKEN`, provider keys, bot tokens, OAuth apps, Tavily, Groq, sandbox keys |
| `config.yaml` | Structure | `llm.*`, `agent.*`, `salience.*`, `mcp_servers`, `messaging.*`, `contacts.*`, `authz.*`, `workspace.*`, `proactive.*`, `workbench.*`, `media.*`, `providers.*` |

Every field and environment variable is in
[docs/configuration.md](docs/configuration.md).

### Policy example

Rules are regexes over the tool name, optionally over the canonical JSON parameters.
First match wins. Anything they do not cover falls through to the built-in ladder.

```yaml
authz:
  rules:
    - tool_pattern: "gmail__send_email"
      decision: approve
      note: "Sends leave Aether"

    - tool_pattern: "linkedin__create_post"
      param_pattern: '"visibility":"public"'
      decision: deny
      note: "Never post publicly without a separate review"

  approval_ttl_hours: 24
```

Provider tools are named `app__tool` (`gmail__send_email`, `github__create_issue`). MCP
tools are `server__tool`. Native tools keep their own names. The built-in ladder is
fail-safe: an unrecognized tool parks. See [docs/policy.md](docs/policy.md).

### Model slots

A named model carries its provider as `provider/model`; a bare id uses `llm.provider`.

| Slot | Key | Values |
| --- | --- | --- |
| Main | `llm.model` | `claude-opus-5`, `openai/gpt-5.1`, `ollama/llama3.3` |
| Images | `llm.image_model` | empty = off, `default` = main, or a named model |
| Audio | `llm.audio_model` | empty = off, `default` = main, or a named model |
| Video | `llm.video_model` | empty = off, `default` = main, or a named model |
| First pass | `llm.salience_model` | `off`, `default`, or a cheaper model |
| Memory search | `llm.embedding_model` | a Gemini embedding model, or `none` |

`base` is accepted as a legacy spelling of `default`. See [docs/models.md](docs/models.md).

## Security model and what we do not claim

Aether asks for a lot of access, so it states plainly where the guarantees end.

- **Encryption at rest.** AES-256-GCM with a server-held key. It protects stored history
  against a database-level compromise such as a leaked backup. It does not protect against
  a compromised server process.
- **Secrets.** The same encrypted store. Provider tokens use a separate keyring so they
  can be rotated; the audit log stores a parameter digest, never values.
- **Model providers.** They see prompts, recent context, tool results, and files in a
  perception call. Choose a provider and retention policy you accept; there is no
  end-to-end encryption.
- **Sandboxes.** Receive only the files a job needs, never the database URL, encryption
  key, or provider tokens.
- **Web content.** Fetched pages and searches cannot change policy; they reach the profile
  only as a proposal you confirm.
- **Screen capture.** Perception only: a screenshot is described, and no action tools are
  registered for the screen.
- **Free-tier hosting.** Schedules persist and re-arm on boot, but do not fire while the
  process is asleep.
- **Single owner.** `AETHER_TOKEN` is shared by the API, web chat, and companion. Aether is
  not multi-tenant.

What we do not claim: no independent security audit, no end-to-end encryption, no
protection from a compromised host, no guarantee that a model provider excludes your
data from training, and no hosted service-level agreement. Report vulnerabilities through
[SECURITY.md](SECURITY.md), not a public issue.

## Architecture

```mermaid
flowchart TB
    subgraph A["Aether: one asyncio process"]
        UI["Panel, WebSocket chat,<br/>Telegram, Discord"]
        Loop["Agent loop"]
        LLM["LLM providers"]
        Tools["Tool registry<br/>native, providers, MCP"]
        Gate["Policy, approvals, audit"]
        Memory["Memory and workspace"]
        Worker["Scheduler and background worker"]
    end

    DB[("Postgres<br/>encrypted rows")]
    Out["Apps, MCP servers, sandboxes"]

    UI --> Loop
    Loop --> LLM
    Loop --> Tools
    Loop --> Gate
    Loop --> Memory
    Worker --> Loop
    Memory --> DB
    Gate --> DB
    Tools --> Out
    Worker --> Out
```

A full component and persistence walkthrough is in
[docs/architecture.md](docs/architecture.md).

### Repository map

| Path | Purpose |
| --- | --- |
| `src/aether/agent/` | Agent loop, native tools, prompts, traces, config and app walks |
| `src/aether/api/` | REST endpoints for the panel, companion, and transfers |
| `src/aether/authz/` | Deterministic policy, approvals, hash-chained audit |
| `src/aether/chat/`, `src/aether/connectors/` | WebSocket chat, MCP host, tool registry, Telegram, Discord, attachments, screen vision |
| `src/aether/llm/`, `src/aether/providers/` | LLM adapters, model registry, embeddings, direct app providers, OAuth |
| `src/aether/memory/`, `src/aether/workspace/` | Encrypted events, entities, vectors, salience, Markdown workspace |
| `src/aether/media/` | Media store, image generation, video edits, frames, voice |
| `src/aether/migrations/` | Forward-only SQL migrations |
| `src/aether/proactive/`, `src/aether/skills/` | Background worker, budgets, and the follow-up, research, and media skills |
| `src/aether/scheduler/`, `src/aether/routines/` | Persisted scheduled actions and event-triggered instructions |
| `src/aether/workbench/` | Sandbox adapters and the routing ladder |
| `webapp/` | React and Tailwind panel source, built into `src/aether/web/` |
| `tests/`, `companion/`, `docs/` | Unit, integration, and browser tests; Wayland capture CLI; guides |

## How it compares

| Attribute | Aether | What to check in an alternative |
| --- | --- | --- |
| Where it runs | Your machine or your cloud account | Vendor-hosted or self-hosted |
| Pre-action policy | Deterministic code, first match wins | Whether policy is a prompt or enforced code |
| Approvals | Web chat, Telegram, Discord | Where approvals land and whether they expire |
| Audit | SHA-256 chain, verifiable in chat | Whether logs are append-only and checkable |
| Integrations | Native providers plus MCP | Whether your specific apps are covered |
| Models | Anthropic, OpenAI, Gemini, NVIDIA NIM, Ollama | Whether you can bring a key and route per role |

## Roadmap

Shipped:

- [x] Deterministic policy gate, one-tap approvals, and hash-chained audit with `/verify`
- [x] Gmail, Calendar, Drive, Docs, GitHub, and LinkedIn providers
- [x] Telegram and Discord chat surfaces, all carrying approvals
- [x] Cross-app memory with entity resolution and semantic recall
- [x] Background worker with budgets, verdicts, self-pause, and a sandbox ladder
- [x] Image generation, video edits, voice transcription, and the web panel

Planned, in no particular order:

- [ ] A dedicated secrets manager for the encryption key
- [ ] Provider zero-data-retention options
- [ ] Screen capture beyond Wayland
- [ ] A hosted public demo

## Contributing

Pull requests are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first. The unit suite
runs with no network and no database; integration and browser tests are opt-in.

Participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Report security
issues through [SECURITY.md](SECURITY.md), not a public issue.

## License

[MIT](LICENSE). Built on FastAPI, asyncpg, Pydantic, the official Anthropic, OpenAI,
Google GenAI, and MCP SDKs, python-telegram-bot, py-cord, and a React and Vite panel.

## FAQ

**Does it run on my laptop?**

Yes. You need Python 3.12+, Node 22 to build the panel, and Postgres (local or free Neon).
Screen capture needs Wayland and `grim`; everything else is platform-independent.

**Which data goes to which provider?**

Your LLM sees prompts, context, tool results, and files in a perception call. Embeddings go
to Gemini, searches to Tavily, image prompts to Pollinations or Gemini, and voice notes to
Groq when no audio model is set. Sandboxes receive only their task files. OAuth tokens stay
in the encrypted store.

**What happens if the server restarts?**

Encrypted rows persist. The scheduler re-arms, a running background task returns to
pending, and approvals keep their expiry clock. An in-flight model turn is lost and not
retried automatically. Chat and panel settings continue to override `config.yaml`.

**What does it cost on free tiers?**

Neon has a free Postgres tier, Render has a free web tier with 750 instance-hours and
spin-down, Tavily has free search credits, and Modal and Vercel have free starting credits.
Model tokens are billed by your provider; GitHub's free plan includes 2,000 Actions minutes
for the batch queue. Terms change, so check current pricing.
