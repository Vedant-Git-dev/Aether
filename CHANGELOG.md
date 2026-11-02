# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
There are no tagged releases yet; entries below describe the current unreleased state.

## [Unreleased]

### Added

- Deterministic authorization policy with user rules and a fail-safe built-in ladder.
- One-tap approvals on the web panel, Telegram, and Discord, with a configurable expiry.
- SHA-256 hash-chained audit log, verifiable with `/verify`.
- Encrypted memory: events, entities, identity handles, notes, and workspace files.
- Gmail, Google Calendar, Google Drive, Google Docs, GitHub, and LinkedIn providers.
- MCP host for stdio and HTTP servers, including scheduled poll tools.
- Background worker with follow-up, research, and media skills, daily budgets, and
  self-pause on repeated bad outcomes.
- Sandbox routing across Modal, Vercel, and a GitHub Actions batch queue.
- Encrypted media store, image generation, fixed-operation video editing, frame
  extraction, and voice transcription.
- React and Vite web panel with activity, approvals, tasks, memory, audit, traces, policy,
  apps, contacts, and settings.
- Wayland screen-capture companion, perception only.
- Configuration through `config.yaml`, the Settings panel, and chat commands, with stored
  overrides and reset to the YAML baseline.
- Documentation for quick start, configuration, policy, channels, models, workbench,
  architecture, and troubleshooting.

### Changed

- Direct provider connections replaced the earlier app broker.
- Provider tokens moved to a separate encryption keyring with rotation support.

### Removed

- Slack integration.
- App broker integration and its MCP entry.

### Security

- AES-256-GCM at rest with additional authenticated data bound to each row and column.
- Audit records store parameter digests, never parameter values.
- Sandbox jobs receive only their task files and never Aether's secrets.

## [0.1.0] - Unreleased

Initial development version.
