<div align="center">

# Aether

### A personal agent you can give write access to, because it shows its work.

**The model proposes. Code decides. The record proves it.**

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/Vedant-Git-dev/Aether/actions/workflows/ci.yml/badge.svg)](https://github.com/Vedant-Git-dev/Aether/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)
[![Self-hosted](https://img.shields.io/badge/self--hosted-yes-success.svg)](#installation)

[**Install**](#installation) · [**Quick start**](docs/quickstart.md) · [**Configuration**](docs/configuration.md) · [**Architecture**](docs/architecture.md) · [**Security**](#security-model-and-what-we-do-not-claim)

</div>

---

Most assistants make you choose: keep the agent read-only and useless, or hand it your
accounts and hope. **Aether is built for the part they skip.**

It is a self-hosted agent that connects to your apps, remembers what is happening across
all of them, and takes actions on your behalf, through native integrations and any MCP
server. But before *anything* runs, deterministic code checks it against a policy **you**
wrote. Reads go ahead. Anything that sends, changes, or deletes waits for **one tap** in
chat. Every decision lands in a hash-chained audit log you can verify with `/verify`.

```text
You       Reply to Sam's email and confirm Friday at 10:00.

Aether    I can send that, but it leaves Aether. Approve it first.
          [ Approve ]   [ Deny ]   [ Read the full text ]

You       Approve

Aether    Sent. The decision record shows the rule that matched and your approval.
```

## Get started

**Set up the real thing.** A keyboard-driven wizard asks the questions, generates your
secrets, and writes a ready config:


<summary><b>On Linux/macOS</b></summary>
<br>

```bash
git clone https://github.com/Vedant-Git-dev/Aether.git
cd Aether
./scripts/setup.sh
```

<summary><b>On Windows (PowerShell)</b></summary>
<br>

```powershell
git clone https://github.com/Vedant-Git-dev/Aether.git
cd Aether
.\scripts\setup.bat
```

**Just want to look around first?** Run the real panel against fake in-memory data. No
database, no API key, nothing saved:

```bash
git clone https://github.com/Vedant-Git-dev/Aether.git && cd Aether
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cd webapp && npm ci && npm run build && cd ..
python tests/e2e/dev_server.py
```

Open `http://127.0.0.1:8731`, paste the token `secret` under **Settings > Access**, and
try the approval flow. Full details are in [Installation](#installation).

---

## Why Aether

- **Policy decides, not the model.** Regular expressions on tool names and call
  parameters, evaluated before execution. There is no LLM in the decision path.
- **One-tap approvals.** Higher-stakes calls park in the web panel, Telegram, or Discord
  with the exact action and its parameters.
- **Tamper-evident audit.** Each record commits to every record before it with SHA-256.
  `/verify` re-walks the chain and reports the first break.
- **Cross-app memory.** Events are deduplicated, people are resolved across handles, and
  recall is semantic when the free embedding model is configured.
- **Bring your own model.** Anthropic, OpenAI, Google Gemini, NVIDIA NIM, or a local
  Ollama model, with separate slots for images, audio, and video.
- **Yours to host.** One asyncio process and Postgres. No hosted instance, no vendor in
  the middle.

## A day with Aether

On Monday you tell a recruiter you will send your availability by Thursday. Thursday is
busy, and the thread goes quiet.

| | What happens |
| --- | --- |
| **Without Aether** | The promise stays in your head until it is too late. |
| **With Aether** | The follow-up skill finds the promise in memory (an event with a marker such as "I will" or "follow up"), drafts from its words, and proposes `gmail__send_email`. The policy parks the send, an approval card arrives with the recipient, subject, and body, and you tap **Approve**. |

Before firing, the skill re-checks that the source event still exists. If the thread
moved on, the draft is dropped and you are told why. The audit row records the rule, the
decision, and who approved it.

With the background worker off, the same thing happens when you ask in chat. The gate is
identical.

> [!TIP]
> The same decision appears on the **Audit** page with a `chain ok` badge, and in the
> expanded **Decision Trace** with every tool call and its ruling.

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
- **One-tap approvals.** Approve or deny from the panel, Telegram, or Discord. Requests expire after 24 hours by default.
- **Tamper-evident audit.** SHA-256 chain over every decision, verified with `/verify`.
- **Contact allowlists.** A cross-app list plus per-app lists for Gmail, Telegram, and Discord. Non-allowlisted senders are dropped at ingestion.
- **Security changes park.** Contacts, authz, messaging, and MCP changes always wait for approval, even from chat.

### Memory and understanding

- **Deduplicated event memory.** A repeated poll result is stored once.
- **Cross-platform identity.** The same person under different handles is merged into one entity.
- **Salience scoring.** A first-pass model, or the main model, decides what is worth mentioning. It can be turned off independently.
- **A workspace you confirm.** Markdown identity, tone, preferences, and durable memory. Inferred profile entries wait until you say yes.
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
- **Three ways to configure.** `config.yaml`, the Settings panel, or `/config` in chat. Chat changes persist and can be reset to the YAML baseline.
- **Self-host anywhere.** One asyncio process and Postgres. A Render blueprint is included.
- **Persistent schedules.** Scheduled actions and routines survive restarts and re-arm on boot.

---

## Installation

Aether needs **Python 3.12+** and a **Postgres** database. A free [Neon](https://neon.tech)
database works, and so does a local one. **Node 22+** is only needed when you build the
panel yourself; the deploy engine builds it on Render.

> [!NOTE]
> There is no hosted instance and no SQLite mode. Everything runs on your machine or in
> your own cloud account.

<details>
<summary><b>Requirements by platform</b></summary>

| Component | Linux | Windows (PowerShell) |
| --- | --- | --- |
| Python 3.12+ | `sudo apt-get install -y python3 python3-venv` | `winget install Python.Python.3.12` |
| Postgres | `sudo apt-get install -y postgresql` | `winget install PostgreSQL.PostgreSQL` |
| Node 22+ (local panel only) | `sudo apt-get install -y nodejs npm` | `winget install OpenJS.NodeJS.LTS` |

Use your distro's package manager when it is not `apt` (`dnf`, `pacman`, `apk`). On
macOS: `brew install python@3.12 postgresql node`. Node 22+ may need NodeSource or `nvm`
on older distributions.

The setup wizard checks the Python version and installs the packages it needs to
run and to verify real values: `rich`, `prompt_toolkit`, `httpx`, `asyncpg`, and
the provider SDKs (`anthropic`, `openai`, `google-genai`), into a private venv
when the system Python refuses `pip`. It does not install Python or Postgres for
you.

</details>

### 1. Install and configure

```bash
git clone https://github.com/Vedant-Git-dev/Aether.git
cd Aether
./scripts/setup.sh
```

<details>
<summary><b>Windows (PowerShell, or <code>scripts\setup.bat</code> from <code>cmd.exe</code>)</b></summary>

```powershell
git clone https://github.com/Vedant-Git-dev/Aether.git
cd Aether
.\scripts\setup.bat
```

</details>

The wizard asks its questions with arrow keys, Space, and Enter. Secrets are masked, and
no question needs a typed number or y/n. It writes your answers, the generated secrets,
and a ready `config.generated.yaml` to `~/.aether-setup`.

For a scripted install, point at an answers file. `--yes` accepts every default
that has one, but required values still have to come from the environment or the
answers file, and a missing one fails the run before anything is written:

```bash
./scripts/setup.sh --answers answers.toml
./scripts/setup.sh --yes
```

```powershell
.\scripts\setup.bat --answers answers.toml
.\scripts\setup.bat --yes
```

### 2. Deploy

Choosing **Deploy to Render** in the wizard runs the deploy engine at the end of
setup, so there is no second command. It uses the Render API key you paste to
create or update the service, waits for the first build, verifies `/healthz` and
the panel, and registers the uptime monitor when you asked for one.

To update an existing deployment later:

```bash
./scripts/deploy.sh --answers ~/.aether-setup/answers.env
```

```powershell
.\scripts\deploy.bat --answers "$env:USERPROFILE\.aether-setup\answers.env"
```

Open the deployed URL, paste the token under **Settings > Access**, and send `/status`.
It reports the resolved models, pending approvals, and queue depth without a model call.

> [!CAUTION]
> **Back up `~/.aether-setup/secrets.env`.** Losing `AETHER_ENCRYPTION_KEY` makes every
> stored message, memory, and token unrecoverable.

### Run locally instead

The wizard's generated config and secrets are plain files and stay in the state
directory, so they never overwrite a `config.yaml` you already have.

```bash
set -a; . ~/.aether-setup/secrets.env; set +a
AETHER_CONFIG=~/.aether-setup/config.generated.yaml aether
```

<details>
<summary><b>Windows (PowerShell)</b></summary>

```powershell
Get-Content "$env:USERPROFILE\.aether-setup\secrets.env" | ForEach-Object {
  $k, $v = $_ -split '=', 2
  if ($k -and $v) { Set-Item -Path "Env:$k" -Value $v }
}
$env:AETHER_CONFIG = "$env:USERPROFILE\.aether-setup\config.generated.yaml"
aether
```

</details>

For a local Postgres, choose **existing** for the database question and paste the local
connection string (for example `postgresql:///aether`).

The full question list, `--answers`, `--yes`, `--dry-run`, and `--no-animation` are in
[docs/hosted-setup.md](docs/hosted-setup.md). Prefer to do every step by hand? Follow
the [manual install](docs/quickstart.md).

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

<details>
<summary><b>Windows (PowerShell)</b></summary>

```powershell
git clone https://github.com/Vedant-Git-dev/Aether.git
cd Aether
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
cd webapp; npm ci; npm run build; cd ..
python tests/e2e/dev_server.py
```

</details>

Expected: `Uvicorn running on http://127.0.0.1:8731`. Open it and paste the token `secret`
under **Settings > Access**. The data is fake and nothing is saved.

### First connectors

Connect from the **Apps** page or the chat walk:

```text
/apps add gmail
```

A pasted value is consumed before the model sees it, stored encrypted, and never echoed
back. MCP servers can be declared in `config.yaml`; see
[docs/configuration.md](docs/configuration.md).

### Optional services

| Capability | Needs |
| --- | --- |
| Web search | `TAVILY_API_KEY`. Without it, `web_search` refuses and `web_fetch` still works. |
| Voice transcription | `GROQ_API_KEY` |
| Background jobs | Modal, Vercel, or a GitHub Actions batch queue |
| Semantic memory | `GEMINI_API_KEY` with a Gemini embedding model |
| Screen capture | Wayland, `grim`, and an image model |

---

## Configuration

Secrets live in `.env`. Structure and policy live in `config.yaml`. Settings changed from
chat or the panel are stored in Postgres and override the YAML until reset.

| File | Holds | Examples |
| --- | --- | --- |
| `.env` | Secrets | `DATABASE_URL`, `AETHER_ENCRYPTION_KEY`, `AETHER_TOKEN`, provider keys, bot tokens, OAuth apps, Tavily, Groq, sandbox keys |
| `config.yaml` | Structure | `llm.*`, `agent.*`, `salience.*`, `mcp_servers`, `messaging.*`, `contacts.*`, `authz.*`, `workspace.*`, `proactive.*`, `workbench.*`, `media.*`, `providers.*` |

Every field and environment variable is documented in
[docs/configuration.md](docs/configuration.md).

### Write your own policy

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
tools are `server__tool`. Native tools keep their own names.

> [!IMPORTANT]
> The built-in ladder is fail-safe: **an unrecognized tool parks.** See
> [docs/policy.md](docs/policy.md).

### Model slots

A named model carries its provider as `provider/model`. A bare id uses `llm.provider`.

| Slot | Key | Values |
| --- | --- | --- |
| Main | `llm.model` | `claude-opus-5`, `openai/gpt-5.1`, `ollama/llama3.3` |
| Images | `llm.image_model` | empty = off, `default` = main, or a named model |
| Audio | `llm.audio_model` | empty = off, `default` = main, or a named model |
| Video | `llm.video_model` | empty = off, `default` = main, or a named model |
| First pass | `llm.salience_model` | `off`, `default`, or a cheaper model |
| Memory search | `llm.embedding_model` | a Gemini embedding model, or `none` |

`base` is accepted as a legacy spelling of `default`. See [docs/models.md](docs/models.md).

---

## Security model and what we do not claim

Aether asks for a lot of access, so it states plainly where the guarantees end.

| Area | What you get |
| --- | --- |
| **Encryption at rest** | AES-256-GCM with a server-held key. Protects stored history against a database-level compromise such as a leaked backup. Does **not** protect against a compromised server process. |
| **Secrets** | The same encrypted store. Provider tokens use a separate keyring so they can be rotated. The audit log stores a parameter digest, never values. |
| **Model providers** | They see prompts, recent context, tool results, and files in a perception call. Choose a provider and retention policy you accept; there is no end-to-end encryption. |
| **Sandboxes** | Receive only the files a job needs, never the database URL, encryption key, or provider tokens. |
| **Web content** | Fetched pages and searches cannot change policy. They reach the profile only as a proposal you confirm. |
| **Screen capture** | Perception only. A screenshot is described, and no action tools are registered for the screen. |
| **Free-tier hosting** | Schedules persist and re-arm on boot, but do not fire while the process is asleep. |
| **Single owner** | `AETHER_TOKEN` is shared by the API, web chat, and companion. Aether is not multi-tenant. |

**What we do not claim:** no independent security audit, no end-to-end encryption, no
protection from a compromised host, no guarantee that a model provider excludes your
data from training, and no hosted service-level agreement.

> [!WARNING]
> Report vulnerabilities through [SECURITY.md](SECURITY.md), not a public issue.

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

<details>
<summary><b>Repository map</b></summary>

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

</details>

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

**Shipped**

- [x] Deterministic policy gate, one-tap approvals, and hash-chained audit with `/verify`
- [x] Gmail, Calendar, Drive, Docs, GitHub, and LinkedIn providers
- [x] Telegram and Discord chat surfaces, all carrying approvals
- [x] Cross-app memory with entity resolution and semantic recall
- [x] Background worker with budgets, verdicts, self-pause, and a sandbox ladder
- [x] Image generation, video edits, voice transcription, and the web panel

**Planned, in no particular order**

- [ ] A dedicated secrets manager for the encryption key
- [ ] Provider zero-data-retention options
- [ ] Screen capture beyond Wayland
- [ ] A hosted public demo

## FAQ

<details>
<summary><b>Does it run on my laptop?</b></summary>

Yes. You need Python 3.12+, Node 22 to build the panel, and Postgres (local or free
Neon). Screen capture needs Wayland and `grim`; everything else is platform-independent.

</details>

<details>
<summary><b>Which data goes to which provider?</b></summary>

Your LLM sees prompts, context, tool results, and files in a perception call. Embeddings
go to Gemini, searches to Tavily, image prompts to Pollinations or Gemini, and voice
notes to Groq when no audio model is set. Sandboxes receive only their task files. OAuth
tokens stay in the encrypted store.

</details>

<details>
<summary><b>What happens if the server restarts?</b></summary>

Encrypted rows persist. The scheduler re-arms, a running background task returns to
pending, and approvals keep their expiry clock. An in-flight model turn is lost and not
retried automatically. Chat and panel settings continue to override `config.yaml`.

</details>

<details>
<summary><b>What does it cost on free tiers?</b></summary>

Neon has a free Postgres tier, Render has a free web tier with 750 instance-hours and
spin-down, Tavily has free search credits, and Modal and Vercel have free starting
credits. Model tokens are billed by your provider. GitHub's free plan includes 2,000
Actions minutes for the batch queue. Terms change, so check current pricing.

</details>

## Contributing

Pull requests are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first. The unit suite
runs with no network and no database; integration and browser tests are opt-in.

Participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Report security
issues through [SECURITY.md](SECURITY.md), not a public issue.

## License

[MIT](LICENSE). Built on FastAPI, asyncpg, Pydantic, the official Anthropic, OpenAI,
Google GenAI, and MCP SDKs, python-telegram-bot, py-cord, and a React and Vite panel.

<div align="center">

**If Aether earns your trust, a ⭐ helps others find it.**

</div>
