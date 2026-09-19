---
name: boot-md
description: "Run BOOT.md on gateway startup"
homepage: https://docs.aether.ai/automation/hooks#boot-md
metadata:
  {
    "aether":
      {
        "emoji": "🚀",
        "events": ["gateway:startup"],
        "requires": { "config": ["workspace.dir"] },
        "install": [{ "id": "bundled", "kind": "bundled", "label": "Bundled with Aether" }],
      },
  }
---

# Boot Checklist Hook

Runs `BOOT.md` at Gateway startup once per distinct configured agent workspace,
if the file exists there. Agents sharing a workspace do not run the same checklist
again. Enable with `aether hooks enable boot-md`, then restart the Gateway.
