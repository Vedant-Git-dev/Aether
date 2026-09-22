# @aether/memory-lancedb

Official LanceDB-backed long-term memory plugin for Aether.

This plugin adds persistent memory tools backed by LanceDB, vector search, auto-recall, and auto-capture.

## Install

```bash
aether plugins install @aether/memory-lancedb
```

Restart the Gateway after installing or updating the plugin.

## What it provides

- `memory_store`
- `memory_recall`
- `memory_forget`
- LanceDB vector storage and hybrid memory retrieval.

## Configure

Use the memory plugin docs for embedding provider setup, storage paths, indexing, and recall behavior:

- <https://docs.aether.ai/plugins/memory-lancedb>

## Package

- Plugin id: `memory-lancedb`
- Package: `@aether/memory-lancedb`
- Enforced minimum Aether host (`aether.install.minHostVersion`): `>=2026.5.31`
- Enforced plugin API compatibility (`aether.compat.pluginApi`): `>=2026.9.3`

The installer checks these ranges independently. Both must be satisfied.
