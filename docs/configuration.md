# Configuration reference

Aether splits configuration in two on purpose:

- Secrets go in `.env` (or the encrypted store, if pasted through the panel or chat).
- Structure and policy go in `config.yaml`.

Both are read at boot. Settings changed from the Settings panel or `/config` are
stored in Postgres and override the YAML. `config.yaml`, or the built-in defaults when
no file exists, remains the baseline you can reset to.

## Precedence

For a secret name, Aether resolves in this order:

1. A value pasted through the panel or the `/apps` walk, stored encrypted.
2. The process environment.
3. `.env`, re-read on demand.

For a setting, the order is:

1. A stored override from chat or the panel.
2. `config.yaml`.
3. The built-in default in `src/aether/config.py`.

`/config reset <path>` removes an override and returns that path to the YAML or
default value.

## Environment variables

### Required

| Name | Purpose |
| --- | --- |
| `DATABASE_URL` | Postgres connection string. Aether runs its migrations on boot. |
| `AETHER_ENCRYPTION_KEY` | Base64 of 32 random bytes. Encrypts content at rest. Losing it loses the data. |
| `AETHER_ENCRYPTION_KEY_ID` | Short label for the active key, such as `2026-10`. Written into every new sealed value. Defaults to `k1`. |
| `AETHER_ENCRYPTION_KEY_PREVIOUS` | Previous content key, only during rotation. Values written under it stay readable. |
| `AETHER_ENCRYPTION_KEY_PREVIOUS_ID` | Label for the previous content key. Required when the previous key is set. |

Content keys carry a version label. A rotation sets the new key as active and the old one as previous: old rows stay readable, new writes use the new version. Keyed blind indexes (address lookups, dedup tokens) are derived from the active key, so a rotation also needs a re-index pass before those lookups see old rows again. That pass ships with the storage migration tooling.

See [sealed-storage.md](sealed-storage.md) for the plaintext allowlist and the sealer.
| `AETHER_TOKEN` | Shared token for the API, web chat, and companion uploads. |

### Core options

| Name | Default | Purpose |
| --- | --- | --- |
| `PORT` | `8000` | HTTP port. `aether` binds `0.0.0.0`. |
| `LOG_LEVEL` | `INFO` | Python logging level. |
| `AETHER_PUBLIC_URL` | empty | This deployment's public address. Required for the GitHub Actions batch queue. |
| `AETHER_DEV_EPHEMERAL_KEY` | unset | Set to `1` for a throwaway instance with a key that changes every boot. Never for real data. |
| `AETHER_CONFIG` | `config.yaml` | Path to a different config file. |

### Provider tokens

| Name | Purpose |
| --- | --- |
| `AETHER_TOKEN_ENCRYPTION_KEY` | Base64 key for provider OAuth tokens. Separate from the content key. |
| `AETHER_TOKEN_ENCRYPTION_KEY_ID` | A short label for the active token key, such as `2026-10`. |
| `AETHER_TOKEN_ENCRYPTION_KEY_PREVIOUS` | Previous token key, for rotation. |
| `AETHER_TOKEN_ENCRYPTION_KEY_PREVIOUS_ID` | Label for the previous token key. |

Rotate token keys with:

```bash
python -m aether.providers.rotate_tokens --check
```

Without `AETHER_TOKEN_ENCRYPTION_KEY`, direct provider connections stay disabled. The
rest of Aether still boots.

### LLM providers

Set the key for the provider you use. Several can be set at once because each model
slot may route to a different provider.

| Name | Purpose |
| --- | --- |
| `ANTHROPIC_API_KEY` | Anthropic |
| `OPENAI_API_KEY` | OpenAI |
| `GEMINI_API_KEY` | Google Gemini, and the free embedding model |
| `NVIDIA_API_KEY` | NVIDIA NIM |
| `NVIDIA_BASE_URL` | Defaults to `https://integrate.api.nvidia.com/v1` |
| `OLLAMA_BASE_URL` | Defaults to `http://localhost:11434/v1` |

### Chat surfaces

| Name | Purpose |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Telegram bot, from BotFather. |
| `DISCORD_BOT_TOKEN` | Discord bot token. |

Enable the surface in `config.yaml` as well. A token without the toggle, or the toggle
without the token, logs why it was skipped and boots without it.

### OAuth apps

Only needed for direct provider connections. The `/apps` walk can also collect these
one at a time.

| Name | Purpose |
| --- | --- |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Google OAuth client for Gmail, Calendar, Drive, and Docs. |
| `GOOGLE_PICKER_API_KEY`, `GOOGLE_PICKER_APP_ID` | Google Picker, used by Drive and Docs. |
| `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` | GitHub OAuth App. A fine-grained personal token also works. |
| `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET` | LinkedIn OAuth app. Tokens last about 60 days. |

### Tools and sandboxes

| Name | Purpose |
| --- | --- |
| `TAVILY_API_KEY` | Web search. Without it, `web_search` refuses in plain words. `web_fetch` still works. |
| `GROQ_API_KEY` | Voice-note transcription with `whisper-large-v3`. |
| `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` | Modal sandbox, the default workbench. |
| `VERCEL_TOKEN`, `VERCEL_TEAM_ID`, `VERCEL_PROJECT_ID` | Vercel sandbox, the fallback. |
| `GITHUB_ACTIONS_TOKEN` | GitHub Actions batch queue. Also needs `workbench.actions_repo` and the public URL. |

Most of these can be pasted in the panel under **Settings > Credentials**, where they
are stored encrypted and never shown back.

## `config.yaml`

Every section is optional. A missing file means all defaults, and Aether boots with the
web panel and no external connectors.

### `llm`

| Key | Default | Notes |
| --- | --- | --- |
| `provider` | `anthropic` | `anthropic`, `openai`, `gemini`, `ollama`, or `nvidia` |
| `model` | `claude-opus-5` | A bare id, or `provider/model` to override the provider |
| `image_model` | empty | Empty = off, `default` = main model, or a named model |
| `audio_model` | empty | Same |
| `video_model` | empty | Same |
| `salience_model` | empty | `off` disables the first pass, `default` uses the main model |
| `embedding_model` | `gemini-embedding-001` | Gemini only; `none` disables semantic recall |
| `max_tokens` | `16000` | Reply ceiling. Do not lower it to save time. |

### `agent`

| Key | Default | Notes |
| --- | --- | --- |
| `tick_seconds` | `30` | How often the loop checks its queue and due polls |
| `max_tool_iterations` | `12` | Tool rounds per turn |
| `daily_surface_cap` | `20` | Maximum of today's events included in the prompt context |
| `turn_timeout_seconds` | `600` | One reply's hard ceiling |
| `max_searches_per_day` | `25` | Shared by chat and background work |
| `quiet_hours` | empty | `"23:00-08:00"`, local time, wraps midnight |
| `quiet_urgent_salience` | `8.0` | Only observations at or above this break quiet hours |

### `salience`

| Key | Default | Notes |
| --- | --- | --- |
| `threshold` | `6.0` | Score needed before an observation is worth mentioning |
| `rate_cap_per_hour` | `20` | Events from one source above this hourly count are pre-gated as noise |

### `mcp_servers`

A list. Each entry has:

| Key | Default | Notes |
| --- | --- | --- |
| `name` | required | Becomes the tool prefix: `name__tool` |
| `transport.type` | `stdio` | `stdio` or `http` |
| `transport.command` | none | For stdio |
| `transport.args` | `[]` | For stdio |
| `transport.env` | `{}` | Values may reference secrets as `$NAME` |
| `transport.url` | none | For HTTP |
| `transport.headers` | `{}` | Values may reference secrets as `$NAME` |
| `poll_tools` | `[]` | `{tool, args, every_minutes}` |
| `enabled` | `true` | |

The legacy MCP entry named `composio` is ignored at boot.

### `messaging`

```yaml
messaging:
  telegram:
    enabled: false
  discord:
    enabled: false
```

### `contacts`

| Key | Default | Notes |
| --- | --- | --- |
| `mode` | `off` | `enforce` filters every surface; `off` does not |
| `allowlist` | `[]` | Cross-app entries: `{platform, handle}`. `platform: "*"` matches everywhere |
| `gmail` | empty slice | `{mode, allowlist}`; Gmail's list also filters inbox reads |
| `telegram` | empty slice | `{mode, allowlist}` |
| `discord` | empty slice | `{mode, allowlist}` |

An app's `mode: null` follows the section default. `platform: email` matches Gmail
senders.

### `authz`

| Key | Default | Notes |
| --- | --- | --- |
| `rules` | `[]` | List of `{tool_pattern, param_pattern, decision, note}` |
| `approval_ttl_hours` | `24` | How long a parked call stays decidable |

`tool_pattern` and `param_pattern` are Python regular expressions, matched with
`search`. `decision` is `allow`, `approve`, or `deny`. See the
[policy guide](policy.md).

### `workspace`

| Key | Default | Notes |
| --- | --- | --- |
| `enabled` | `true` | |
| `path` | `aether-workspace` | One-time import source; the files then live in Postgres |
| `encrypted` | `true` | Retained for older config files; the database store always seals |

Files: `IDENTITY.md`, `SOUL.md`, `AGENTS.md`, `USER.md`, `MEMORY.md`, and
`memory/YYYY-MM-DD.md`. Each is one sealed row in `workspace_files`, bound to
its row id, with a keyed blind token for the name. On the first boot after
upgrading, a file-backed workspace at `path` is imported and its files are
removed; nothing readable stays on disk. Edit them in Settings, Workspace
files, or through the workspace tools. Export and import use a passphrase.

### `proactive`

| Key | Default | Notes |
| --- | --- | --- |
| `enabled` | `false` | The worker reads this live each cycle |
| `interval_minutes` | `30` | |
| `max_tasks_per_day` | `3` | |
| `max_tokens_per_day` | `100000` | |
| `max_sandbox_minutes_per_day` | `20` | |
| `notify_daily_cap` | `5` | Results delivered per day |
| `skills` | `[followups, research, media]` | Unknown names are ignored |

### `workbench`

| Key | Default | Notes |
| --- | --- | --- |
| `provider` | `modal` | `modal`, `vercel`, `github_actions`, or `none` |
| `monthly_minutes_cap` | `1500` | Applies to the batch queue |
| `job_timeout_minutes` | `10` | One job's ceiling |
| `actions_repo` | empty | `owner/name` for the batch queue |
| `actions_workflow` | empty | Workflow file in that repository |
| `actions_ref` | `main` | Branch to dispatch on |

### `media`

| Key | Default | Notes |
| --- | --- | --- |
| `storage_quota_mb` | `200` | Encrypted media in Postgres |
| `image_provider` | `pollinations` | `pollinations`, `gemini`, or `none` |
| `max_images_per_day` | `10` | |
| `max_sandbox_jobs_per_day` | `20` | Frame extraction and video edits |
| `max_prompt_chars` | `500` | Image prompts travel to the image service |

### `providers`

| Key | Default | Notes |
| --- | --- | --- |
| `enabled` | `true` | Direct app connections |
| `gmail.poll_minutes` | `5` | Gmail polling interval |

## Setting values from chat

```text
/config show
/config show agent
/config set agent.tick_seconds 10
/config add contacts.allowlist telegram @friend
/config add authz.rules gmail__send_email approve "sends leave Aether"
/config remove authz.rules gmail__send_email
/config reset agent.tick_seconds
```

One-line writes are deterministic; no model is called. Personal tuning under `agent.*`,
`salience.*`, `llm.*`, `proactive.*`, `workbench.*`, and `providers.*` applies
immediately. `llm.*` changes restart the process to load the new provider. Security
sections (`contacts`, `authz`, `messaging`, `mcp_servers`) park for a one-tap approval.
Bare `/config` starts a guided walk.

## Validation

A bad YAML shape fails boot with a pydantic error naming the field. A bad regex fails
boot with the rule that could not compile. Neither is silently ignored.
