# Changelog

All notable changes to Aether are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-11-21

First tagged release. Aether is a self-hosted personal agent that checks every
tool call against a policy you wrote, parks anything that changes the world for
one tap, and records the decision in a hash-chained audit log.

### Added

**Trust and control**

- Deterministic authorization: first-match regex rules over tool names and call
  parameters, evaluated in code with no model in the path. Unknown tools fail
  safe and park for approval.
- One-tap approvals on Telegram and Discord with the exact call, plus the same
  pending request and audit record in the web panel. Approvals expire after 24
  hours by default.
- Hash-chained audit log: every record commits to the one before it with
  SHA-256, and `/verify` re-walks the chain and reports the first break.
  Parameter values are never stored, only their digests.
- Contact allowlists, cross-app and per-app for Gmail, Telegram and Discord,
  with non-allowlisted senders dropped at ingestion.
- Changes to contacts, authorization, messaging and MCP always park for
  approval.
- Storage guard: a plaintext allowlist for every column, checked at boot and in
  CI, plus a manifest per native tool declaring its risk, decision and
  provenance before the tool may boot.
- Typed tool outcomes recorded in traces, so each call's status is part of the
  decision record.

**Memory and understanding**

- Deduplicated event memory with cross-platform entity resolution.
- Sealed storage at rest with AAD binding per table, column and row, and key
  rotation that keeps old rows readable.
- Salience scoring on its own model slot, and semantic recall with a
  configurable embedding model.
- A resident vector matrix behind recall: ranking happens in memory, and only
  the cited text and metadata are read by id.
- A Markdown workspace whose inferred profile entries wait for confirmation.
- Attachments (images, PDFs, documents, audio and video frames) become
  encrypted events, with summaries and facts built on top.
- Explicit controls to inspect, search and forget memory.

**Reach**

- Native Gmail, Calendar, Drive, Docs, GitHub and LinkedIn tools, with OAuth,
  scope auditing, and a reauth path when a grant is missing a scope.
- Anthropic, OpenAI, Gemini, Groq, NVIDIA NIM and local Ollama models, with
  prompt cache breakpoints and token-usage accounting where the provider
  supports them.
- At-least-once provider polling with persisted cursors and a Gmail reconcile
  sweep for missed messages.
- Any stdio or HTTP MCP server, including scheduled poll tools.
- Telegram and Discord chat surfaces that carry approvals, a React web panel,
  and WebSocket chat.
- A Wayland screen-vision companion that captures the screen on request.
- Background follow-ups, research and media jobs in a workbench that runs on
  Modal, Vercel, GitHub Actions, or a local fake.
- Image generation, a fixed video-editing operation set (trim, concat, speed,
  mute, extract audio, resize, convert), and voice transcription.
- Alerts and reminders from natural language, with immediate or digest
  delivery, a daily cap, retries and optional model phrasing.

**Operations and setup**

- One asyncio process and Postgres. Forward-only migrations from `001` to `027`
  apply at boot.
- Idle discipline: an idle tick reads nothing, non-urgent work runs in one
  background burst every 15 minutes, provider and scheduler state live in
  memory, and the panel pulls its lists with ETags instead of polling.
- An optional-work budget: the vector backfill pauses when the day's query or
  response allowance is spent, and interactive work never consults it.
- A database outage circuit: after three connection failures the breaker opens,
  calls fail immediately, one notice is sent when it opens and one when it
  closes, incoming messages stay queued, and actions fail closed.
- Three ways to configure: `config.yaml`, the Settings panel, and `/config` in
  chat, with chat changes persisted and resettable to the YAML baseline.
- A setup wizard with an answers file, a deploy engine, a Render blueprint and
  Neon support.
- A model per role: reasoning, salience, embeddings, images, audio and video.

### Changed

- The package version is derived from the git tag through `hatch-vcs`, with a
  `0.0.0` fallback for source trees without `.git`. `pip show aether-agent`
  reports the release that was installed.

### Fixed

- Quiet-hours notes survive a restart in `held_notes` and ship as one batch when
  the window ends; rows are released only after the send.
- A reminder turn that fails no longer consumes the lead. Settlement happens
  after the turn and retries up to three times before giving up.
- Groq sends no token cap, so its per-minute admission works as intended.

[Unreleased]: https://github.com/Vedant-Git-dev/Aether/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Vedant-Git-dev/Aether/releases/tag/v0.1.0
