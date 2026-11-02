# The cloud workbench

Some work should not run on the machine serving Aether: watching a video, extracting
frames, editing media, or running a script a skill composed. The workbench is where
those jobs go.

Routing is deterministic code. The model never chooses a provider.

## The ladder

Providers are tried in this order:

1. The configured default (`workbench.provider`).
2. The remaining providers in the order they were built: Modal, Vercel, GitHub Actions.

Each provider answers three questions for itself:

- `available()`: are its keys present and is its quota usable?
- `accepts(spec)`: is this class of work allowed here?
- `run_job(spec)`: run it, and clean up in `finally`.

A dispatch failure falls through to the next provider that would have taken the job. A
job that started and failed its own way is a real result; it is never retried on another
provider, because the job already happened.

If no provider can take the job, the caller refuses or queues in plain words.

## Job classes

A job carries a sensitivity and a latency class.

| Meta | Values | Default if absent |
|---|---|---|
| `sensitivity` | `sensitive`, `ordinary` | `sensitive` |
| `latency_class` | `interactive`, `background` | `background` |

The fail-safe defaults matter:

- Unlabeled work is treated as sensitive.
- The GitHub Actions batch queue accepts only `ordinary` and `background`. It refuses
  sensitive, interactive, and unlabeled work itself, so the rule lives with the
  provider rather than in a router.
- Modal and Vercel are dedicated ephemeral sandboxes and accept every class.

## Providers

### Modal (default)

- Key pair: `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`.
- One sandbox per job, created for it and terminated after it.
- Debian slim image with `ffmpeg`, so video work needs no runtime install.
- Takes every class of work.

### Vercel (fallback)

- Keys: `VERCEL_TOKEN`, `VERCEL_TEAM_ID`, `VERCEL_PROJECT_ID`.
- Same throwaway shape on a second cloud.
- Exists so the ladder has somewhere to fall through when Modal is out of quota.
- Takes every class of work.

### GitHub Actions batch queue

- Key: `GITHUB_ACTIONS_TOKEN`.
- Also needs `workbench.actions_repo`, `workbench.actions_workflow`, and
  `AETHER_PUBLIC_URL`.
- Runs patient, non-sensitive work in your own repository's workflow queue.
- Metered against `workbench.monthly_minutes_cap` (default 1500, below GitHub's 2000
  free minutes). At 80 percent Aether warns; at 100 percent it stops dispatching there
  until the month turns.
- Each job is one workflow run. The run is deleted after collection.

Full setup, including the two workflow files and the one-time URL exchange, is in
[github-actions-sandbox.md](github-actions-sandbox.md).

## What a sandbox receives

A job is a named script plus its files. It never receives:

- `DATABASE_URL`
- `AETHER_ENCRYPTION_KEY`
- provider API keys
- bot tokens
- the Postgres credentials

The script runs as an argument vector, never through a shell. File names are flat and
validated, so a crafted name has nothing to inject into. The audit record carries the
script's SHA-256 digest, not its text; the text lives encrypted in the task row.

For the batch queue, the repository receives two unguessable one-time URLs: one for the
job bundle, one for the result. Both die on first use and expire within half an hour.
No Aether secret is committed or dispatched.

## Limits

| Setting | Default | Applies to |
|---|---|---|
| `workbench.job_timeout_minutes` | 10 | Every job; a skill may ask for less, never more |
| `workbench.monthly_minutes_cap` | 1500 | Batch queue only |
| `proactive.max_sandbox_minutes_per_day` | 20 | Background jobs |
| `media.max_sandbox_jobs_per_day` | 20 | Frame extraction and video edits |
| `media.max_images_per_day` | 10 | Image generation |
| `media.max_prompt_chars` | 500 | Image prompts |

A job that exceeds its timeout comes back as a real timed-out result. It is not silently
retried.

## What runs where

| Work | Provider path |
|---|---|
| Background follow-up, research | No sandbox needed; uses tools directly |
| Video frame extraction | Ladder, image model for the frames |
| Video edit | Ladder, requires an `ffmpeg`-capable provider |
| Media description job | Ladder if a sandbox is needed |
| Batch, patient, non-sensitive | GitHub Actions if configured |

Video editing has no degraded fallback: if no provider on the ladder has `ffmpeg`, the
edit is refused before any bytes travel.

## Failure behavior

- **No provider available.** The caller is told plainly. Nothing runs.
- **Dispatch failed.** Fall through to the next provider. If none remains, the last
  error is surfaced.
- **Job ran and failed.** A real result with a nonzero exit. No fallback.
- **Timeout.** A real result marked timed out. No fallback.
- **Meter unreadable.** The batch provider marks itself unavailable rather than
  guessing about the quota.

## Settings

These can be changed from the panel's Settings page or `/config`:

```text
/config set workbench.provider modal
/config set workbench.job_timeout_minutes 10
/config set workbench.actions_repo your-name/your-repo
/config set workbench.actions_workflow aether-batch.yml
/config set workbench.actions_ref main
```

`workbench.*` is a tuning root, so it applies without an approval. Sandbox keys are
credentials: paste them under **Settings > Credentials**.
