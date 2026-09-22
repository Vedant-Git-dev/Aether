# @aether/tokenjuice

Official Tokenjuice output compaction plugin for Aether.

Tokenjuice compacts noisy `exec` and `bash` tool results after commands run, before the result is fed back into the active agent session. It does not rewrite commands, rerun commands, or change exit codes.

## Install

```bash
aether plugins install @aether/tokenjuice
```

Restart the Gateway after installing or updating the plugin.

## Enable

```bash
aether config set plugins.entries.tokenjuice.enabled true
```

Equivalent:

```bash
aether plugins enable tokenjuice
```

## Docs

- https://docs.aether.ai/tools/tokenjuice

## Package

- Plugin id: `tokenjuice`
- Package: `@aether/tokenjuice`
- Minimum Aether host: `2026.5.28`
