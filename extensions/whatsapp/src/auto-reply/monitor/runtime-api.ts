// Whatsapp API module exposes the plugin public contract.
export { resolveIdentityNamePrefix } from "aether/plugin-sdk/agent-runtime";
export { formatInboundEnvelope } from "aether/plugin-sdk/channel-inbound";
export { resolveInboundSessionEnvelopeContext } from "aether/plugin-sdk/channel-inbound";
export { createChannelMessageReplyPipeline } from "aether/plugin-sdk/channel-outbound";
export {
  isControlCommandMessage,
  shouldComputeCommandAuthorized,
} from "aether/plugin-sdk/command-detection";
export { resolveChannelContextVisibilityMode } from "../config.runtime.js";
export { getAgentScopedMediaLocalRoots } from "aether/plugin-sdk/media-runtime";
export type LoadConfigFn = typeof import("../config.runtime.js").getRuntimeConfig;
export {
  buildHistoryContextFromEntries,
  type HistoryEntry,
} from "aether/plugin-sdk/reply-history";
export { resolveSendableOutboundReplyParts } from "aether/plugin-sdk/reply-payload";
export {
  resolveChunkMode,
  resolveTextChunkLimit,
  type getReplyFromConfig,
  type ReplyPayload,
} from "aether/plugin-sdk/reply-runtime";
export {
  resolveInboundLastRouteSessionKey,
  type resolveAgentRoute,
} from "aether/plugin-sdk/routing";
export { logVerbose, shouldLogVerbose, type getChildLogger } from "aether/plugin-sdk/runtime-env";
export { resolvePinnedMainDmOwnerFromAllowlist } from "aether/plugin-sdk/security-runtime";
export { resolveMarkdownTableMode } from "aether/plugin-sdk/markdown-table-runtime";
export { jidToE164, normalizeE164 } from "../../text-runtime.js";
