# Copilot Proxy (Aether plugin)

Provider plugin for the **Copilot Proxy** VS Code extension.

## Enable

This bundled plugin is enabled by default. If you previously disabled it, re-enable it:

```bash
aether plugins enable copilot-proxy
```

Restart the Gateway after enabling.

## Authenticate

```bash
aether models auth login --provider copilot-proxy --set-default
```

## Notes

- Copilot Proxy must be running in VS Code.
- Base URL must include `/v1`.
