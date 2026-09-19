# Lobster (plugin)

Adds the `lobster` agent tool as an **optional** plugin tool.

## Install

```bash
aether plugins install @aether/lobster
```

Restart the Gateway after installing or updating the plugin.

## What this is

- Lobster is a standalone workflow shell (typed JSON-first pipelines + approvals/resume).
- This plugin integrates Lobster with Aether _without core changes_.

## Enable

Because this tool can trigger side effects (via workflows), it is registered with `optional: true`.

Enable it in an agent allowlist:

```json
{
  "agents": {
    "list": [
      {
        "id": "main",
        "tools": {
          "allow": [
            "lobster" // plugin id (enables all tools from this plugin)
          ]
        }
      }
    ]
  }
}
```

## Using `aether.invoke` (Lobster → Aether tools)

Some Lobster pipelines may include a `aether.invoke` step to call back into Aether tools/plugins (for example: `gog` for Google Workspace, `gh` for GitHub, `message.send`, etc.).

For this to work, the Aether Gateway must expose the tool bridge endpoint and the target tool must be allowed by policy:

- Aether provides an HTTP endpoint: `POST /tools/invoke`.
- The request is gated by **gateway auth** (e.g. `Authorization: Bearer …` when token auth is enabled).
- The invoked tool is gated by **tool policy** (global + per-agent + provider + group policy). If the tool is not allowed, Aether returns `404 Tool not available`.

### Allowlisting recommended

To avoid letting workflows call arbitrary tools, set a tight allowlist on the agent that will be used by `aether.invoke`.

Example (allow only a small set of tools):

```jsonc
{
  "agents": {
    "list": [
      {
        "id": "main",
        "tools": {
          "allow": ["lobster", "web_fetch", "web_search", "gog", "gh"],
          "deny": ["gateway"],
        },
      },
    ],
  },
}
```

Notes:

- If `tools.allow` is omitted or empty, it behaves like "allow everything (except denied)". For a real allowlist, set a **non-empty** `allow`.
- Tool names depend on which plugins you have installed/enabled.

## Security

- Runs Lobster in process via the published `@aether/lobster/core` runtime.
- Does not manage OAuth/tokens.
- Uses timeouts, stdout caps, and strict JSON envelope parsing.

## Docs

- https://docs.aether.ai/tools/lobster

## Package

- Plugin id: `lobster`
- Tool: `lobster`
- Package: `@aether/lobster`
- Minimum Aether host: `2026.4.25`
