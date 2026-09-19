// Private runtime barrel for the bundled Microsoft Teams extension.
// Keep this barrel thin and aligned with the local extension surface.

export { DEFAULT_ACCOUNT_ID } from "aether/plugin-sdk/account-id";
export type { AllowlistMatch } from "aether/plugin-sdk/allow-from";
export {
  mergeAllowlist,
  resolveAllowlistMatchSimple,
  summarizeMapping,
} from "aether/plugin-sdk/allow-from";
export type {
  BaseProbeResult,
  ChannelDirectoryEntry,
  ChannelGroupContext,
  ChannelMessageActionName,
  ChannelOutboundAdapter,
} from "aether/plugin-sdk/channel-contract";
export type { ChannelPlugin } from "aether/plugin-sdk/channel-core";
export { logTypingFailure } from "aether/plugin-sdk/channel-outbound";
export { createChannelPairingController } from "aether/plugin-sdk/channel-pairing";
export { resolveToolsBySender } from "aether/plugin-sdk/channel-policy";
export { createChannelMessageReplyPipeline } from "aether/plugin-sdk/channel-outbound";
export {
  PAIRING_APPROVED_MESSAGE,
  buildProbeChannelStatusSummary,
  createDefaultChannelRuntimeState,
} from "aether/plugin-sdk/channel-status";
export {
  buildChannelKeyCandidates,
  normalizeChannelSlug,
  resolveChannelEntryMatchWithFallback,
  resolveNestedAllowlistDecision,
} from "aether/plugin-sdk/channel-targets";
export type {
  GroupPolicy,
  GroupToolPolicyConfig,
  MSTeamsChannelConfig,
  MSTeamsCloudName,
  MSTeamsConfig,
  MSTeamsReplyStyle,
  MSTeamsTeamConfig,
  MarkdownTableMode,
  AetherConfig,
} from "aether/plugin-sdk/config-contracts";
export { isDangerousNameMatchingEnabled } from "aether/plugin-sdk/dangerous-name-runtime";
export { resolveDefaultGroupPolicy } from "aether/plugin-sdk/runtime-group-policy";
export { withFileLock } from "aether/plugin-sdk/file-lock";
export { keepHttpServerTaskAlive } from "aether/plugin-sdk/channel-outbound";
export {
  detectMime,
  extensionForMime,
  extractOriginalFilename,
  getFileExtension,
} from "aether/plugin-sdk/media-runtime";
export { resolveChannelMediaMaxBytes } from "aether/plugin-sdk/account-helpers";
export { loadOutboundMediaFromUrl } from "aether/plugin-sdk/outbound-media";
// Deprecated media-legacy-projection surface; the re-export stays until the
// compat record's removeAfter window expires (deleted in retirement PR 4).
export { buildMediaPayload } from "aether/plugin-sdk/reply-payload";
export type { ReplyPayload } from "aether/plugin-sdk/reply-payload";
export type { PluginRuntime } from "aether/plugin-sdk/runtime-store";
export type { RuntimeEnv } from "aether/plugin-sdk/runtime";
export type { SsrFPolicy } from "aether/plugin-sdk/ssrf-runtime";
export { fetchWithSsrFGuard } from "aether/plugin-sdk/ssrf-runtime";
export { normalizeStringEntries } from "aether/plugin-sdk/string-normalization-runtime";
export { chunkTextForOutbound } from "aether/plugin-sdk/text-chunking";
export { DEFAULT_WEBHOOK_MAX_BODY_BYTES } from "aether/plugin-sdk/webhook-ingress";
export { setMSTeamsRuntime } from "./src/runtime.js";
