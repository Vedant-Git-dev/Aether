# @aether/acpx

Official ACP runtime backend for Aether.

ACPx lets Aether run external coding harnesses through the Agent Client Protocol while Aether still owns sessions, channels, delivery, permissions, and Gateway state.

## Install

```bash
aether plugins install @aether/acpx
```

Restart the Gateway after installing or updating the plugin.

## What it provides

- ACP-backed agent runtime sessions.
- Plugin-owned session and transport management.
- MCP bridge helpers for Aether tools and plugin tools.
- Static runtime assets used by the ACP process bridge.

## Configure

Use the ACP docs for harness-specific setup, permission modes, and model/runtime selection:

- https://docs.aether.ai/tools/acp-agents-setup
- https://docs.aether.ai/tools/acp-agents

## Package

- Plugin id: `acpx`
- Package: `@aether/acpx`
- Minimum Aether host: `2026.4.25`
