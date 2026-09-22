// Narrow Matrix monitor helper seam.
// Keep monitor internals off the broad package runtime-api barrel so monitor
// tests and shared workers do not pull unrelated Matrix helper surfaces.

export type { NormalizedLocation } from "aether/plugin-sdk/channel-inbound";
export type { PluginRuntime, RuntimeLogger } from "aether/plugin-sdk/plugin-runtime";
export type { BlockReplyContext, ReplyPayload } from "aether/plugin-sdk/reply-runtime";
export type { AetherConfig } from "aether/plugin-sdk/config-contracts";
export type { RuntimeEnv } from "aether/plugin-sdk/runtime";
export {
  addAllowlistUserEntriesFromConfigEntry,
  buildAllowlistResolutionSummary,
  canonicalizeAllowlistWithResolvedIds,
  patchAllowlistUsersInConfigEntries,
  summarizeMapping,
} from "aether/plugin-sdk/allow-from";
export {
  createReplyPrefixOptions,
  createTypingCallbacks,
} from "aether/plugin-sdk/channel-outbound";
export { formatLocationText, toLocationContext } from "aether/plugin-sdk/channel-inbound";
export { getAgentScopedMediaLocalRoots } from "aether/plugin-sdk/media-local-roots";
export { logInboundDrop } from "aether/plugin-sdk/channel-inbound";
export { logTypingFailure } from "aether/plugin-sdk/channel-outbound";
export { buildChannelKeyCandidates } from "aether/plugin-sdk/channel-targets";
