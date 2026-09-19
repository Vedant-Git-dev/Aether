# @aether/comfy-provider

Official ComfyUI image, video, and music generation provider plugin for
Aether.

## Install

```bash
aether plugins install @aether/comfy-provider
aether gateway restart
```

## Configure

Local ComfyUI workflows do not require credentials. Comfy Cloud workflows use
`COMFY_API_KEY` or `COMFY_CLOUD_API_KEY`.

Full workflow, model, and provider configuration:

- https://docs.aether.ai/providers/comfy

## Package

- Plugin id: `comfy`
- Package: `@aether/comfy-provider`
- Minimum Aether host: `2026.7.2`
