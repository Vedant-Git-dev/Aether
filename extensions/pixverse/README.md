# @aether/pixverse-provider

Official PixVerse video generation provider plugin for Aether.

This plugin registers PixVerse as a `video_generate` provider for text-to-video and image-to-video workflows.

## Install

```bash
aether plugins install @aether/pixverse-provider
```

Restart the Gateway after installing or updating the plugin.

## Configure

Store your PixVerse API key in Aether config or expose the supported environment variable to the Gateway. Then select PixVerse as a video generation provider.

Full setup and model/provider examples:

- https://docs.aether.ai/providers/pixverse

## Package

- Plugin id: `pixverse`
- Package: `@aether/pixverse-provider`
- Minimum Aether host: `2026.5.26`
