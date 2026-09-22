// Telegram plugin module implements send behavior.
export { requireRuntimeConfig } from "aether/plugin-sdk/plugin-config-runtime";
export { resolveMarkdownTableMode } from "aether/plugin-sdk/markdown-table-runtime";
export type { AetherConfig } from "aether/plugin-sdk/config-contracts";
export type { PollInput } from "aether/plugin-sdk/media-runtime";
export {
  buildOutboundMediaLoadOptions,
  getImageMetadata,
  normalizePollInput,
  probeVideoDimensions,
} from "aether/plugin-sdk/media-runtime";
export { loadWebMedia } from "aether/plugin-sdk/web-media";
