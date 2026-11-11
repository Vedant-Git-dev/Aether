---
title: "Setup wizard and deployment"
summary: "The keyboard-driven setup wizard, the answers file, and the deploy engine for a local or Render instance."
read_when:
  - You are running scripts/setup.sh or scripts/setup.bat
  - You are deploying to Render
  - You want a non-interactive or scripted install
---

# Setup wizard and deployment

`scripts/setup.sh` (Linux and macOS) and `scripts/setup.bat` (Windows 10/11)
launch the interactive, keyboard-driven setup wizard. The wizard replaces the
old typed prompts: every option is drawn with a circle indicator, arrow keys
move the highlight, Space toggles multi-select rows, and Enter confirms. No
question ever asks you to type a number or a y/n. Secrets are entered masked.

The wizard collects every answer, generates the encryption material, and
writes its artifacts to `~/.aether-setup` (or the `--state-dir` you choose):

| File | What it is |
| --- | --- |
| `answers.toml` | the answers, in the canonical TOML form |
| `answers.env` | the same answers as `KEY=value`, for the deploy engine |
| `secrets.env` | the secrets (owner-only); back this up |
| `config.generated.yaml` | the non-secret `config.yaml` for the service |
| `render.yaml` | a Render blueprint with the `sync: false` secrets |

Deployment is `scripts/deploy.sh` (Linux/macOS) and `scripts/deploy.bat`
(Windows). Choosing **Deploy to Render** runs the platform's engine
non-interactively at the end of setup: it creates or updates the Render
service, waits for the first deploy, verifies `/healthz` and the panel, and
creates an UptimeRobot monitor when monitoring was chosen. The same scripts
with `--answers` update an existing deployment later.

The wizard is implemented in `src/aether/setup`:

| Module | What it holds |
| --- | --- |
| `brand.py` | the wordmark, tagline, gradient and timing (change the brand in one place) |
| `capabilities.py` | TTY, color and circle-glyph detection |
| `theme.py` | the rich console and the prompt_toolkit style |
| `prompts.py` | the circle prompts (**single**, **multi**, text and masked input) |
| `catalogs.py` | the choices, read from the repository's own files |
| `plan.py` | the answers dataclass and the TOML / env-file I/O |
| `stages.py` | the ordered question flow |
| `wizard.py` | back-navigation, non-interactive mode and persistence |
| `requirements.py` | the dependency preflight the launchers run (check + install) |
| `cli.py` | argument parsing and the entry point |

## Quick start

From a clone of this repository:

```bash
./scripts/setup.sh
```

```bat
scripts\setup.bat
```

Both derive the repo root from their own location, so they can be run from any
working directory. The wizard opens with a short animated Aether wordmark
(under 2.5 seconds), then asks the questions below.

## Requirements

| System | Needs |
| --- | --- |
| Linux | Python 3.12 or newer; `curl` and `base64` for the deploy step |
| Windows | Windows 10/11, Python 3.12 or newer; built-in PowerShell and `curl.exe` for the deploy step |

Before asking anything, `scripts/setup.sh` / `scripts/setup.bat` run a
preflight (`python -m aether.setup.requirements --mode setup`):

- it checks the Python version and the packages the wizard and its live checks
  need: `rich`, `prompt_toolkit`, `httpx`, `asyncpg`, `pydantic`,
  `pydantic-settings`, `pyyaml`, and the provider SDKs (`anthropic`, `openai`,
  `google-genai`);
- missing packages are installed with `pip`; if the system Python refuses pip
  (PEP 668's externally-managed marker), the launcher creates a private
  virtual environment at `~/.aether-setup/venv` (override with
  `AETHER_SETUP_VENV`) and installs them there;
- if Python itself is missing or too old, it prints the exact install command
  for the platform and stops.

The deploy engine (`scripts/deploy.sh` / `scripts/deploy.bat`) runs the same
preflight in deploy mode. `curl` is needed on both platforms to talk to the
Render API. `base64` is needed on Linux and macOS only: `deploy.sh` uses the
`base64` command to generate `AETHER_ENCRYPTION_KEY` and `AETHER_TOKEN` and to
encode `config.yaml` into `AETHER_CONFIG_B64`. Windows does all of that through
PowerShell's `[Convert]::ToBase64String`, so `scripts/deploy.bat` requires only
`curl.exe` (built into Windows 10/11) and never a `base64` executable. The
preflight tries to install a missing tool with the platform's package manager
when it can, and otherwise prints the command to run. Python is optional there;
the engine uses it for key generation and the UptimeRobot email flow, but has
fallbacks.

The packages are declared in `pyproject.toml`; running `pip install -e .`
installs them for an existing environment. The wizard never needs jq. A local
target builds the panel with `npm` at the end, so Node 22+ is needed for a
source checkout (a Render deploy builds it on the service; an installed wheel
already carries it). The wizard uses the network to verify what it can: a
database connection, a model key's model-list call, a Gemini embedding, and the
Telegram or Discord bot identity. A check that cannot run reports a plain error
and the fix.

## Keyboard

- **Arrow keys** (`↑`/`↓`) move the highlight between options.
- **Space** toggles a multi-select row; on a single-choice question it
  confirms the highlighted option.
- **Enter** confirms the current question and advances.
- **Esc** or **Backspace** goes back one question, where it makes sense.
- **Ctrl+C** at any step stops cleanly, prints a message, and writes nothing.

Single-choice rows show `○`, with `●` in green on the option under the cursor.
Multi-select rows show `●` for every toggled option, so several can be green at
once. The row under the cursor is highlighted separately (bold and underlined,
with a cyan pointer), so "where I am" never gets confused with "what I picked".
Each multi-select shows a count of selected items and a hint line:
`↑↓ move · space toggle · enter continue`. Blank lines separate the question,
the options, the count and the hint so the block is easy to scan.

The green is rich's plain `green`, which stays readable on both dark and light
terminal themes. When color is off (a `NO_COLOR` environment, or a non-TTY),
the green is dropped and the `●`/`○` marks still show the state. When the
console font cannot render the circles, the wizard falls back to `(*)` and
`( )` and says so. The wordmark fades violet to pink; change the two colors in
`brand.py` to re-skin it.

## The questions, in order

A question with a default accepts a bare Enter. A required value with no
default must be entered, and a blank, placeholder or invalid answer is re-asked.
The multi-select questions start with nothing selected; use Space to add.

1. **Where to run** - a local instance (default) or deploy to Render.
2. **Database** - `neon` (default, recommended), `existing` (paste a connection
   string), or `render`.
3. **Model provider** - read from `provider: Literal[...]` in
   `src/aether/config.py` (`anthropic`, `openai`, `gemini`, `ollama`, `nvidia`,
   `groq`).
   Then **main model**, read from `config.example.yaml`.
4. **Semantic memory** - on (Gemini embeddings) or off. On requires a Gemini key.
5. **Optional services** (multi-select) - read from the "Optional services"
   table in `docs/quickstart.md`: web search, voice-note transcription, image
   generation, background jobs. Screen capture is deliberately omitted.
6. **Direct apps** (multi-select) - from the `AppDefinition` blocks in
   `src/aether/providers/*.py`: gmail, calendar, drive, docs, github, linkedin.
7. **Chat channels** (multi-select) - from `src/aether/apps_catalog.py`:
   telegram, discord. At least one is required.
8. **Render details** - only for the Render target: plan, the Render API key,
   service name, region, deploy source, and uptime monitoring (on asks for an
   alert email). The API key is checked with a workspace read. The service name
   is refused when `<name>.onrender.com` already answers or when the workspace
   already has a service with that name.
9. **Credentials** follow the choices, one masked ask each. A required value is
   re-asked until it is valid; an invalid database, model key, embedding key or
   bot token shows a plain error and the fix.

When a question needs a key, the wizard opens the page that issues it in the
default browser and prints the same link in the hint, so a machine without a
browser still shows it. `AETHER_SETUP_NO_BROWSER=1` turns the automatic open
off.

When the Render target is chosen, the wizard runs the deploy engine
non-interactively from the answers it just wrote. The user runs only
`scripts/setup.sh` (or `scripts/setup.bat`); there is no second script.

A non-interactive run (`--answers FILE` or `--yes`) validates every required
answer before writing anything. If any are missing or invalid it lists them all
and exits non-zero.

## Answers file and non-interactive mode

`--answers FILE` reads either the canonical `answers.toml` or the legacy
`KEY=value` file the old scripts used:

```toml
setup_target = "render"
database_mode = "neon"
llm_provider = "openai"
llm_model = "openai/gpt-5.1"
semantic_memory = false
services = ["Web search"]
apps = ["gmail", "calendar"]
channels = ["telegram"]
plan = "free"
service_name = "aether"
region = "oregon"
deploy_source = "fork"
repo_url = "https://github.com/you/Aether"
uptime_mode = "email"
alert_email = "you@example.com"
workbench_provider = "none"
image_provider = "none"
```

Secrets (`RENDER_API_KEY`, `DATABASE_URL`, `OPENAI_KEY`, `TELEGRAM_TOKEN`, and
so on) come from the environment or the same file; only the non-secret answers
are written back to `answers.toml`.

```bash
./scripts/setup.sh --answers answers.toml
./scripts/setup.sh --yes
```

`--yes` accepts every default that has one and never prompts; required values
still have to come from the environment or an answers file. `--answers` fills
what the file provides and defaults the rest, so CI and scripted installs work
without a TTY. If there is no TTY and neither flag is given, the wizard fails
with a message that names both flags.

Secrets may live in the answers file (keep it owner-only) or in the
environment; they are never written back to `answers.toml`. `--dry-run`
collects the answers and prints them without writing anything, and
`--no-animation` skips the intro. The animation also skips cleanly on a
non-TTY and when `NO_COLOR` is set.

On a machine that already has setup state, a normal run exits without changing
anything. Add `--force` to run again.

## Deploying

The wizard writes `answers.env` and `secrets.env`, then the deploy engine uses
them:

```bash
./scripts/deploy.sh --answers ~/.aether-setup/answers.env
```

```bat
scripts\deploy.bat --answers %USERPROFILE%\.aether-setup\answers.env
```

The deploy engine reads `~/.aether-setup/secrets.env` automatically, so it does
not re-ask for the values the wizard collected. `--dry-run` prints every action
and makes no network call.

## Blocker check: what survives a Render restart

This is the one limitation the wizard states before the plan question.

- **Workspace files live only on disk.** `aether-workspace/` (`IDENTITY.md`,
  `SOUL.md`, `AGENTS.md`, `USER.md`, `MEMORY.md`, `memory/YYYY-MM-DD.md`) is a
  filesystem-backed store (`src/aether/workspace/store.py`). There is no
  Postgres mirror. On boot `ensure_scaffold()` recreates *defaults* when the
  directory is missing, so a disk wipe silently resets identity, personality,
  remembered preferences, and daily notes.
- **Everything else lives in Postgres.** Chat history, memory events, media
  blobs, approvals, scheduler rows, encrypted secrets, and settings changed
  from chat or the panel (`config_overrides`) survive a restart. An `llm.*`
  change causes a self-restart, but the override row is already committed.
- **No other durable disk writes.** Logs go to stderr, media is stored in
  Postgres, temp files used by media editing live inside remote sandboxes, and
  `config.yaml`/`.env` are read-only at runtime.

Therefore:

- **Free plan** - the workspace resets on every spin-down, restart, or redeploy.
  Chat/memory/settings still survive in Postgres.
- **Paid plan + persistent disk** - mount a 1 GB disk at
  `/opt/render/project/src/aether-workspace`. The workspace then survives.
- The wizard chooses the disk automatically for `starter` and the blueprint
  never attaches one to `free`, because Render does not allow it.

## Plan limits (checked November 2026)

| | Render Free | Render Starter | Neon Free |
| --- | --- | --- | --- |
| Cost | $0 | about $7/month | $0 |
| CPU / RAM | 0.1 CPU / 512 MB | 0.5 CPU / 512 MB | usage-based |
| Spin-down | after 15 idle minutes | never | 5-minute idle scale-to-zero |
| Disk | ephemeral, no persistent disk | 1 GB disk at $0.25/GB/month | n/a |
| Limits | 750 instance-hours per workspace per month | none of the free limits | 0.5 GB storage, 100 compute-hours/month |
| Database | free Postgres expires after 30 days (14-day grace) | - | does not expire |

A 5-minute uptime check prevents the free instance from idling out, but it does
not stop Render from restarting the service and it does not preserve the local
disk. The monthly 750 hours are just enough for one always-on service.

Sources for these numbers: Render's free-tier documentation and pricing page,
the compute-plans reference, the persistent-disks documentation, Neon's pricing
page, and UptimeRobot's API and pricing pages (links under References).

## Private repository or fork

Render deploys a private repo or a fork through your Git provider connection. The
script prints this and opens the Blueprint page:

1. Push the repository (or your fork) to GitHub.
2. In Render, choose **New > Blueprint** (or **New > Web Service**).
3. When Render asks for a Git provider, authorize GitHub and grant it access to
   the repository.
4. Pick the repository and branch. Render reads `render.yaml` from the root.
5. Paste the `sync: false` values when prompted.

Both deploy-source choices use the Render API. The **public** choice uses
`https://github.com/Vedant-Git-dev/Aether`; the **fork** choice asks for your own
URL. The chosen repository must be connected to your Render account (Render's
GitHub app installed), because the API cannot attach a repository Render has not
been granted access to. The dashboard route is only the fallback when no Render
API key is given.

## Configuration without committing it

`config.yaml` is gitignored and holds no secrets. Aether reads structure from
`config.yaml` or the path in `AETHER_CONFIG` (`src/aether/config.py`); there is
no environment variable that carries YAML directly. The wizard therefore
generates the YAML, base64-encodes it into `AETHER_CONFIG_B64`, and the deploy
engine adds a build step:

```sh
if [ -n "$AETHER_CONFIG_B64" ]; then
  printf %s "$AETHER_CONFIG_B64" | base64 -d > config.yaml
fi
```

The value is non-secret, lives in Render's settings rather than the repository,
and is applied before the build. An empty value means Aether's defaults. The
generated YAML sets `llm.provider`, `llm.model`, `llm.embedding_model`, the
Telegram/Discord toggles, `media.image_provider`, `workbench.provider`, and
`providers.enabled`.

Secrets always go through Render environment variables (`sync: false` in the
Blueprint, or a direct API call). Nothing secret is written to the repository or
the Blueprint.

## Render creation

- The wizard's Render path requires a Render API key, so the deploy engine does
  everything through the API: it finds the workspace id (`GET /v1/owners`), finds an existing
  service by name (`GET /v1/services?name=...`), creates it (`POST /v1/services`
  with `runtime: python`, `healthCheckPath: /healthz`, `envSpecificDetails`,
  and the environment variables), reads the real URL
  (`GET /v1/services/{id}`), sets `AETHER_PUBLIC_URL`, triggers a deploy
  (`POST /v1/services/{id}/deploys`), and polls the deploy status. The create
  call itself starts Render's "First Deploy". If Render appends a suffix to the
  subdomain because the requested name is taken, a second deploy is needed to
  apply the real `AETHER_PUBLIC_URL`; the deploy engine skips that second deploy when
  no OAuth app or GitHub Actions queue needs the URL. The wizard refuses a name
  whose `onrender.com` subdomain already answers, and also a name the workspace
  already uses, so the exact name is available to the create call.
- Run `scripts/deploy.sh` directly without an API key and it writes a tailored
  `render.yaml`, opens the Blueprint page, and prints the exact values to paste.
  Render prompts for the `sync: false` secrets. The repository's root
  `render.yaml` is the Blueprint Render reads on that fallback route.
- On failure the deploy engine prints the deploy status and recent Render events and
  links to the build log.

## Secrets

- The wizard reads secrets masked (`prompt_toolkit`'s password input) and writes
  them owner-only (`chmod 600` on Linux, `icacls` on Windows). A value is never
  echoed while it is being typed, logged, or committed.
- At the end, the wizard prints `AETHER_TOKEN` once, in full, as the panel
  access token to paste under **Settings > Access**. Every other collected
  secret stays in `secrets.env` (owner-only, `chmod 600`); the wizard does not
  echo them.
- `AETHER_ENCRYPTION_KEY` is 32 random bytes, base64. Losing it makes stored
  data unrecoverable. The wizard says this once and keeps a backup copy in
  `~/.aether-setup/secrets.env`.
- When the deploy engine updates an existing service through the Render API, it
  keeps the encryption key already deployed there. If the local resume file is
  missing, it reads the key from Render instead of replacing it, so old chat,
  memory, and trace rows stay readable. `--rotate-encryption-key` is the only
  way to replace it, and it makes every previously stored row unreadable.
- `AETHER_TOKEN` is a 64-character random value. Anyone holding it can control
  the instance, so it is treated as a shared secret, not a user account.
- The token-encryption keyring (`AETHER_TOKEN_ENCRYPTION_KEY` and its id) is always generated, even with no direct app chosen, so apps can be connected later with `/apps`.
- The wizard writes every collected secret to `~/.aether-setup/secrets.env`
  (owner-only), so the deploy engine does not re-ask. Back it up, and do not
  commit it.

## Verification

The deploy engine checks:

1. `https://<service>.onrender.com/healthz` returns `{"ok":true}`.
2. `/` returns HTTP 200 (a 503 means the panel build is missing).
3. `/api/config` accepts `x-aether-token` (the token is live).
4. The Render boot log shows the database-ready line, which means migrations ran.
5. The UptimeRobot monitor exists and reports up (first check can take 5 minutes).

The agent loop itself has no JSON endpoint. Open the panel, paste the token under
**Settings > Access**, and send `/status`; a deterministic reply means the loop
is running.

## UptimeRobot

- Free plan: 50 monitors, 5-minute checks, email alerts, 10 API requests/minute.
- With an API key the deploy engine creates or updates a **keyword** monitor on
  `/healthz` expecting `ok`, interval 300 seconds, so the free instance stays
  awake. It stores the key only for the run. Alerts go to the email on the
  UptimeRobot account, so use the address you want to be alerted at.
- Without an API key, the deploy engine uses UptimeRobot's agentic flow: it requests a
  proof-of-work challenge (`/agentic/agent-monitor/challenge`), solves it (the
  Windows script in PowerShell, the Linux script with `python3` when present,
  otherwise it opens the browser deep link), submits the monitor, and asks the
  user to click the activation link in their inbox. No account or monitor
  exists until that click.
- The official CLI (`npm install -g @uptimerobot/cli`, Node 22.12+) is an
  alternative for manual use; it needs an API key.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `Application startup failed. DATABASE_URL is not set.` | The service was created without its environment variables. Open the Render service, **Environment**, and add `DATABASE_URL`, `AETHER_ENCRYPTION_KEY`, `AETHER_TOKEN`, `AETHER_CONFIG_B64`, `PYTHON_VERSION=3.12.11`, and `NODE_VERSION=22.12.0`, then **Save and deploy**. Or re-run `scripts/setup.sh --force` and then `scripts/deploy.sh` with the same name; it finds the existing service and updates every variable. The generated secrets are in `~/.aether-setup/secrets.env`. |
| Setup says it already ran | Setup state exists in `~/.aether-setup` (or the `--state-dir` you chose). Add `--force` to update the existing service, or point `--state-dir` at a fresh directory for a separate setup. |
| `/healthz` never turns ok | Check Render logs for `database ready`; a bad `DATABASE_URL` or missing `AETHER_ENCRYPTION_KEY` stops boot. |
| Logs say `trace N undecryptable` or `event N undecryptable` | The row was encrypted with a different `AETHER_ENCRYPTION_KEY` than the one the process is running with. Restore the original key if you have it; otherwise the row is treated as empty. Re-running setup no longer rotates the key. |
| Panel returns 503 `panel not built` | The npm build failed, or the built panel was excluded from the wheel. `src/aether/web/` is gitignored and Hatchling respects `.gitignore`; `pyproject.toml` now pins it with `artifacts = ["src/aether/web/**"]`. Make sure the build command runs `cd webapp && npm ci && npm run build` before `pip install .`, then clear the build cache and redeploy. |
| Embedding 404 `models/none is not found` | The embedding knob used the literal string `none`. Aether's off sentinels are `off`, `default`, and `base`; `none` is treated as a model id. Use `off` to disable or a model such as `gemini-embedding-001` to enable it. The setup wizard writes a model only when memory search is on, and requires a Gemini key then. |
| First build is slow | Normal: `npm ci`, Vite, and `pip install` on a small instance. |
| Free instance asleep | The monitor is the fix; confirm it targets `/healthz` every 5 minutes. |
| Settings reset after restart | Expected on free: `aether-workspace/` is disk-only. Use a paid plan with the disk. |
| OAuth redirect mismatch | Register the exact URI printed: `https://<service>.onrender.com/api/oauth/<provider>/callback`. |
| 30 days later the database is gone | The Render free Postgres expired; use Neon or upgrade. |

## References

- Render Blueprints and `render.yaml`: https://render.com/docs/blueprint-spec
- Render API: https://render.com/docs/api and https://api-docs.render.com
- Render web services, port binding, health checks: https://render.com/docs/web-services
- Render native runtimes (Python and Node/npm availability): https://render.com/docs/native-runtimes
- Render Python version pinning: https://render.com/docs/python-version
- Render free limits: https://render.com/docs/free
- Render persistent disks: https://render.com/docs/disks
- Neon free plan: https://neon.tech/pricing
- UptimeRobot API: https://uptimerobot.com/api
- UptimeRobot no-key agent flow: https://uptimerobot.com/quick-monitor-setup/
- UptimeRobot CLI: https://github.com/uptimerobot/uptimerobot-cli