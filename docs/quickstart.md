# Quick start

This page is the long version of the README's installation guide. It covers the
honest paths:

1. **Try the panel with no database.** Real app, real panel, fake in-memory data.
2. **Set up the agent.** The recommended path is the keyboard-driven setup
   wizard (`scripts/setup.sh` / `scripts/setup.bat`) and the deploy engine
   (`scripts/deploy.sh` / `scripts/deploy.bat`); the manual equivalent follows
   for reference.

Aether has no hosted instance. Everything below runs on your machine or your account.

## Requirements

| Component | Version | Needed for |
| --- | --- | --- |
| Python | 3.12 or newer | Everything |
| Postgres | any recent version; CI runs 18 | Self-hosting |
| Node | 22 or newer | Building the panel |
| `grim` | current | Screen capture on Wayland only |

`python -m venv` is used throughout. Do not use `uv` with this project.

## Path 1: Try the panel with no database

The fixture server is `tests/e2e/dev_server.py`. It is the real FastAPI app, the real
routers, and the real panel, wired to in-memory fakes instead of Postgres. It does not
call a model, run the agent loop, or persist anything.

```bash
git clone https://github.com/Vedant-Git-dev/Aether.git
cd Aether
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cd webapp && npm ci && npm run build && cd ..
python tests/e2e/dev_server.py
```

Expected output:

```text
INFO:     Started server process [12345]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8731 (Press CTRL+C to quit)
```

Open `http://127.0.0.1:8731`. Under **Settings > Access**, paste the token:

```text
secret
```

The fixture data is fake. There is no database and nothing survives a restart. This is
for evaluating the panel and the approval flow, not for connecting real apps.

## Path 2: Set up the agent

### 2a. Use the setup scripts (recommended)

The scripts replace the manual copy-and-edit steps below. The wizard checks and
installs its own Python dependencies, then asks every question with arrow keys,
Space and Enter. Secrets are masked; no question needs a typed number or y/n.

```bash
git clone https://github.com/Vedant-Git-dev/Aether.git
cd Aether
./scripts/setup.sh
```

On Windows, in PowerShell:

```powershell
.\scripts\setup.bat
```

It writes the answers, the generated secrets and a ready config to
`~/.aether-setup` (`answers.toml`, `answers.env`, `secrets.env`,
`config.generated.yaml`, `render.yaml`). Deploy the Render service it configured:

```bash
./scripts/deploy.sh --answers ~/.aether-setup/answers.env
```

On Windows, in PowerShell:
`.\scripts\deploy.bat --answers "$env:USERPROFILE\.aether-setup\answers.env"`.
With a Render API key and your own fork, the deploy engine creates or updates the
service, waits for the first build, verifies `/healthz` and the panel, and creates the
uptime monitor. Without a key (or with the public upstream repo), it writes the
Blueprint and prints the dashboard steps instead.

Open the deployed URL, paste the token under **Settings > Access**, and send
`/status`. Back up `~/.aether-setup/secrets.env`; losing `AETHER_ENCRYPTION_KEY`
makes every stored message, memory, and token unrecoverable.

To run locally instead of on Render, use the wizard's generated files:

```bash
cp ~/.aether-setup/config.generated.yaml config.yaml
set -a; . ~/.aether-setup/secrets.env; set +a
cd webapp && npm ci && npm run build && cd ..
aether
```

For a local Postgres, choose **existing** for the database question and paste the
local connection string (for example `postgresql:///aether`).

The full question list, the `answers.toml` format, `--yes`, `--answers`,
`--dry-run` and `--no-animation` are in
[hosted-setup.md](hosted-setup.md).

### Manual setup (the traditional flow)

Everything from here is the manual equivalent, for when you want to see or change
each step yourself.

### 1. Clone and install

```bash
git clone https://github.com/Vedant-Git-dev/Aether.git
cd Aether
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Expected: pip finishes with `Successfully installed aether-agent-0.1.0 ...`.

### 2. Create the database

Aether runs its own forward-only migrations at boot. You only need an empty database.

Local Postgres:

```bash
createdb aether
```

Free Neon:

1. Create a project at [neon.tech](https://neon.tech).
2. Copy the connection string. Keep `sslmode=require`.

The URL looks like:

```text
postgresql://user:password@host/dbname?sslmode=require
```

Aether needs permission to create tables in the `public` schema. It does not need
superuser.

### 3. Generate the encryption key

```bash
python -c "from aether.memory.crypto import generate_key_b64; print(generate_key_b64())"
```

Expected: one base64 line, 44 characters including padding.

Save it outside the repository. Every stored message, memory, approval, and provider
token is encrypted with it. If you lose it, the data is unrecoverable. If you leak it
alongside a database backup, the encryption no longer helps.

### 4. Write `.env`

```bash
cp .env.example .env
```

Minimum to boot:

```dotenv
DATABASE_URL=postgresql://user:pass@host/dbname?sslmode=require
AETHER_ENCRYPTION_KEY=<the base64 line from step 3>
AETHER_TOKEN=<a long random string>
```

Add one model provider:

```dotenv
ANTHROPIC_API_KEY=sk-ant-...
# or
OPENAI_API_KEY=sk-...
# or
GEMINI_API_KEY=...
# or
NVIDIA_API_KEY=...
```

`AETHER_TOKEN` is a shared secret, not a user account. Anyone with it can read and
change this instance. Use a long random value and do not put it in a URL.

Optional, only if you connect direct apps:

```dotenv
AETHER_TOKEN_ENCRYPTION_KEY=<another base64 32-byte key>
AETHER_TOKEN_ENCRYPTION_KEY_ID=2026-10
```

Without that keyring, Gmail, Calendar, Drive, Docs, GitHub, and LinkedIn connections
stay disabled. The rest of Aether works.

### 5. Write `config.yaml`

```bash
cp config.example.yaml config.yaml
```

The example selects Anthropic and `claude-opus-5`. Change the provider and model if
needed:

```yaml
llm:
  provider: openai
  model: openai/gpt-5.1
```

A bare model id uses `llm.provider`. A value with a slash routes to that provider
directly. The Settings panel's dropdown lists the models each configured key can
reach.

### 6. Build the panel

```bash
cd webapp
npm ci
npm run build
cd ..
```

Expected:

```text
vite v8.x building client environment for production...
../src/aether/web/index.html
../src/aether/web/index-*.css
../src/aether/web/index-*.js
built in ...
```

The build output goes to `src/aether/web/`, which is gitignored. Without it, the API
still runs but `/` returns a plain 503 with the build command.

### 7. Start Aether

```bash
aether
```

Expected:

```text
INFO:     database ready (migrations applied this boot: 001_init, ...)
INFO:     workspace ready at aether-workspace (first run: True)
INFO:     aether running — surfaces: web, agent loop + scheduler live
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

On the first boot, Aether creates the `aether-workspace` directory with `IDENTITY.md`,
`SOUL.md`, `AGENTS.md`, `USER.md`, and `MEMORY.md`, encrypted at rest.

### 8. Check health

```bash
curl http://localhost:8000/healthz
```

Expected:

```json
{"ok":true}
```

`/health` answers the same. Neither endpoint requires the token.

### 9. Open the panel

Open `http://localhost:8000`.

Under **Settings > Access**, paste `AETHER_TOKEN`. The token stays in this browser's
local storage and is sent as the `x-aether-token` header. It is not stored in a URL.

### 10. Send the first message

Send:

```text
/status
```

Expected shape:

```text
Right now:
- main model: claude-opus-5
- reading images: nothing set
- listening to audio: nothing set
- watching video: nothing set
- nothing changed from config.yaml
- no approvals waiting
- nothing queued
```

`/status` is deterministic. It does not call a model, spend tokens, or write memory.
If it replies, the loop is running.

Then ask a normal question, for example:

```text
What can you see right now?
```

A reply means the model key works. A plain reply saying the model is unavailable means
the key is missing, wrong, or the provider rejected it. The panel, audit, and settings
still work in that state.

## First connectors

Direct apps connect from **Apps** or the chat walk:

```text
/apps
/apps add gmail
```

The walk gives you the link or the exact values to paste. A pasted value is consumed
before the model sees it and stored encrypted. It is never echoed back.

MCP servers can be added the same way, or declared in `config.yaml`:

```yaml
mcp_servers:
  - name: weather
    transport:
      type: stdio
      command: npx
      args: ["-y", "mcp-weather"]
```

## Optional services

| Capability | Needs |
| --- | --- |
| Web search | `TAVILY_API_KEY`. Without it, `web_search` refuses; `web_fetch` still works. |
| Voice-note transcription | `GROQ_API_KEY`. Without it, voice notes are kept but not transcribed unless an audio model is set. |
| Background jobs | A key pair for Modal, Vercel, or a GitHub Actions batch queue. |
| Semantic memory | `GEMINI_API_KEY` and `llm.embedding_model`. |
| Image generation | Nothing for Pollinations; `GEMINI_API_KEY` for the Gemini rung. |
| Screen capture | The companion CLI, Wayland, and `grim`. |

## Stopping and restarting

`Ctrl+C` stops the process. Start it again with `aether`. Rows persist. The scheduler
re-arms. A background task that was running is reset to pending. Pending approvals keep
their expiry clock.

If you change `llm.*` from chat or the panel, Aether restarts itself in place to load
the new provider. The process does not leave the container.

## Where to go next

- [Configuration](configuration.md) for every field and environment variable.
- [Policy](policy.md) for rules and the built-in ladder.
- [Channels](channels.md) for Telegram, Discord, and screen capture.
- [Models](models.md) for model slots and provider routing.
- [Troubleshooting](troubleshooting.md) when boot fails.
