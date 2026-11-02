# Hosted setup scripts

`scripts/setup.sh` (Linux) and `scripts/setup.bat` (Windows 10/11) take a new
user from a blank machine to Aether running on a public Render URL with an
uptime monitor. They ask the same questions in the same order, read the same
answers file, call the same Render and UptimeRobot APIs, and end with the same
result. They do not change Aether's application code.

The scripts live next to this page and are safe to run again. A second run finds
the existing Render service and monitor and updates them; it never deletes a
service, a database, or a monitor.

## Quick start

From a clone of this repository:

```bash
./scripts/setup.sh
```

```bat
scripts\setup.bat
```

Both scripts derive the repo root from their own location, so they can be run
from any working directory. They ask the questions below, open the pages you
need, and end with a live URL and a monitor.

## Requirements

| System | Needs |
| --- | --- |
| Linux | bash, coreutils (`base64`, `head`, `date`, `chmod`), `curl` |
| Windows | Windows 10/11, cmd, built-in PowerShell 5.1 |

No Python, Node, or jq is needed on the machine running the script. The scripts
generate the encryption keys themselves. If the Aether package is importable,
the Linux script uses the quickstart's own `generate_key_b64()`; otherwise it
produces the same 32-byte base64 value from `/dev/urandom`.

Windows notes: `setup.bat` uses cmd for the menu flow and the built-in
PowerShell 5.1 for hidden input, HTTP, JSON, random keys, and the UptimeRobot
proof-of-work. Nothing needs to be installed with winget. cmd's delayed
expansion does not carry a literal `!` reliably; if a secret contains one, set it
in the parent shell before launching the script (or use a Render API key and let
the script read it from the environment).

## The questions, in order

Every question is a menu with a default. Multi-select questions take
comma-separated numbers; Enter takes the default.

1. **Database** - `neon` (default, recommended), `existing` (paste a connection
   string), or `render` (with a plain warning that Render's free Postgres
   expires 30 days after creation).
2. **Model provider** - read from `provider: Literal[...]` in
   `src/aether/config.py` (`anthropic`, `openai`, `gemini`, `ollama`, `nvidia`).
   Then **main model** - the example model comes from `config.example.yaml`,
   and the naming rule from `docs/models.md` is printed: a bare id uses
   `llm.provider`, and `provider/model` routes to that provider.
3. **Optional services** (multi-select) - read from the "Optional services"
   table in `docs/quickstart.md`: web search, voice-note transcription, image
   generation, background jobs. Semantic memory is always on (it uses Gemini
   embeddings) and is not offered as a choice. Screen capture is deliberately not
   offered: it needs Wayland, `grim`, and the companion on the user's machine.
4. **Direct apps** (multi-select) - read from the `AppDefinition` blocks in
   `src/aether/providers/*.py`: gmail, calendar, drive, docs, github, linkedin.
5. **Chat channels** (multi-select) - read from the messaging recipes in
   `src/aether/apps_catalog.py`: telegram, discord.
6. **Render plan** - `free` (default) or `starter` (paid). The trade-offs are
   printed before the choice: free is 512 MB / 0.1 CPU, spins down after 15 idle
   minutes, has an ephemeral disk, and shares 750 instance-hours per workspace
   per month; Starter is about $7/month, does not spin down, and can attach a
   persistent disk.
7. **Render details** - service name (default `aether`), region (default
   `oregon`), and deploy source: the public upstream repo (default) or your own
   fork. A fork is required for full Render API automation.
8. **Uptime monitoring** - `api` (default; UptimeRobot API key), `email`
   (UptimeRobot's no-API-key email-confirmation flow), `manual` (print the steps
   and open the page), or `skip`.
9. **Local copy for testing** - `no` (default) or `yes`. Yes prints the Path 2
   steps; it does not start a second deployment.

Credential prompts follow the choices: Render account and API key, database,
model provider key, optional-service keys, OAuth clients (with the exact Google
recipe and redirect URI), Telegram/Discord tokens, and the UptimeRobot key.
Browser pages open automatically (`xdg-open`/`gio` on Linux, `start` on
Windows), with the link printed as a fallback.

## Answers file and non-interactive mode

Both scripts accept the same `KEY=value` file:

```text
DATABASE_MODE=neon
DATABASE_URL=postgresql://user:pass@host/db?sslmode=require
LLM_PROVIDER=openai
LLM_MODEL=openai/gpt-5.1
SERVICES=Web search
APPS=gmail,calendar
CHANNELS=telegram
PLAN=free
SERVICE_NAME=aether
REGION=oregon
DEPLOY_SOURCE=fork
REPO_URL=https://github.com/you/Aether
UPTIME_MODE=api
ALERT_EMAIL=you@example.com
LOCAL_COPY=no
WORKBENCH_PROVIDER=none
IMAGE_PROVIDER=none
```

`SERVICES`, `APPS`, and `CHANNELS` accept the displayed names or 1-based
numbers. Secrets may be placed in the same file (keep it `chmod 600` /
owner-only) or passed through the environment. On Linux:

```bash
./scripts/setup.sh --dry-run --answers answers.env
RENDER_API_KEY=... UPTIME_API_KEY=... ./scripts/setup.sh --answers answers.env
```

On Windows:

```bat
scripts\setup.bat /dry-run --answers answers.env
```

Individual answers can also be passed with `--set KEY=VALUE` (or `/set` on
Windows), repeated as needed. For example:

```bash
./scripts/setup.sh --set SERVICE_NAME=aether --set PLAN=free
```

`--dry-run` / `/dry-run` asks the questions, prints every action, and makes no
network call. Non-interactive mode never prompts; missing secrets are reported
and skipped.

## Blocker check: what survives a Render restart

This is the one limitation the scripts state before the plan question.

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
- The scripts choose the disk automatically for `starter` and never attach one
  to `free`, because Render does not allow it.

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

The Render API route requires the fork to be connected to your Render account;
the API cannot attach an unconnected public repository. That is why the scripts
fall back to the dashboard for the public-repo choice.

## Configuration without committing it

`config.yaml` is gitignored and holds no secrets. Aether reads structure from
`config.yaml` or the path in `AETHER_CONFIG` (`src/aether/config.py`); there is
no environment variable that carries YAML directly. The scripts therefore
generate the YAML, base64-encode it into `AETHER_CONFIG_B64`, and add a build
step:

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

- With a Render API key and your own fork, the script does everything through
  the API: it finds the workspace id (`GET /v1/owners`), finds an existing
  service by name (`GET /v1/services?name=...`), creates it (`POST /v1/services`
  with `runtime: python`, `healthCheckPath: /healthz`, `envSpecificDetails`,
  and the environment variables), reads the real URL
  (`GET /v1/services/{id}`), sets `AETHER_PUBLIC_URL`, triggers a deploy
  (`POST /v1/services/{id}/deploys`), and polls the deploy status.
- Without a key, or with the public upstream repo (which the API cannot attach
  to a workspace), the script writes a tailored `render.yaml`, opens the
  Blueprint page, and prints the exact values to paste. Render prompts for the
  `sync: false` secrets. The repository's root `render.yaml` is the single
  Blueprint Render reads for the public-repo route and for a fork.
- On failure the script prints the deploy status and recent Render events and
  links to the build log.

## Secrets

- Pasted values are read with `read -s` (Linux) or a secure PowerShell prompt
  (Windows); they are never echoed, logged, or committed.
- `AETHER_ENCRYPTION_KEY` is 32 random bytes, base64. Losing it makes stored
  data unrecoverable. The scripts say this once and offer an owner-only backup
  file (`chmod 600` on Linux, `icacls` on Windows).
- `AETHER_TOKEN` is a 64-character random value. Anyone holding it can control
  the instance, so it is treated as a shared secret, not a user account.
- The token-encryption keyring (`AETHER_TOKEN_ENCRYPTION_KEY` and its id) is always generated, even with no direct app chosen, so apps can be connected later with `/apps`.
- A resume copy of the core deployment secrets (encryption key, token, database
  URL, and the token keyring) is kept in `~/.aether-setup/secrets.env`
  (owner-only). Third-party provider keys, the Render API key, and the
  UptimeRobot API key are not written there; they are used for the run only.

## Verification

The scripts check:

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
- With an API key the script creates or updates a **keyword** monitor on
  `/healthz` expecting `ok`, interval 300 seconds, so the free instance stays
  awake. It stores the key only for the run. Alerts go to the email on the
  UptimeRobot account, so use the address you want to be alerted at.
- Without an API key, the script uses UptimeRobot's agentic flow: it requests a
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
| `Application startup failed. DATABASE_URL is not set.` | The service was created without its environment variables. Open the Render service, **Environment**, and add `DATABASE_URL`, `AETHER_ENCRYPTION_KEY`, `AETHER_TOKEN`, `AETHER_CONFIG_B64`, `PYTHON_VERSION=3.12.11`, and `NODE_VERSION=22.12.0`, then **Save and deploy**. Or re-run the fixed script with the same name; it finds the existing service and updates every variable. The generated core secrets are in `~/.aether-setup/secrets.env`. |
| `/healthz` never turns ok | Check Render logs for `database ready`; a bad `DATABASE_URL` or missing `AETHER_ENCRYPTION_KEY` stops boot. |
| Panel returns 503 `panel not built` | The npm build failed, or the built panel was excluded from the wheel. `src/aether/web/` is gitignored and Hatchling respects `.gitignore`; `pyproject.toml` now pins it with `artifacts = ["src/aether/web/**"]`. Make sure the build command runs `cd webapp && npm ci && npm run build` before `pip install .`, then clear the build cache and redeploy. |
| Embedding 404 `models/none is not found` | The embedding knob used the literal string `none`. Aether's off sentinels are `off`, `default`, and `base`; `none` is treated as a model id. Use `off` to disable or the default `gemini-embedding-001`. The setup scripts always write the default and require a Gemini key. |
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