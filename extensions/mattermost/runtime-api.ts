// Private runtime barrel for the bundled Mattermost extension.
// Keep this barrel thin and generic-only.

export type {
  BaseProbeResult,
  ChannelAccountSnapshot,
  ChannelDirectoryEntry,
  ChannelGroupContext,
  ChannelMessageActionName,
  ChannelPlugin,
  ChatType,
  HistoryEntry,
  AetherConfig,
  AetherPluginApi,
  PluginRuntime,
} from "aether/plugin-sdk/core";
export type { RuntimeEnv } from "aether/plugin-sdk/runtime";
export type { ReplyPayload } from "aether/plugin-sdk/reply-runtime";
export type { ModelsProviderData } from "aether/plugin-sdk/models-provider-runtime";
export type {
  BlockStreamingCoalesceConfig,
  ContextVisibilityMode,
  DmPolicy,
  GroupPolicy,
} from "aether/plugin-sdk/config-contracts";
export {
  DEFAULT_ACCOUNT_ID,
  buildChannelConfigSchema,
  createDedupeCache,
  parseStrictPositiveInteger,
  resolveClientIp,
  isTrustedProxyAddress,
} from "aether/plugin-sdk/core";
export { buildComputedAccountStatusSnapshot } from "aether/plugin-sdk/channel-status";
export { createAccountStatusSink } from "aether/plugin-sdk/channel-outbound";
export {
  listSkillCommandsForAgents,
  resolveControlCommandGate,
  resolveStoredModelOverride,
} from "aether/plugin-sdk/command-auth-native";
export { buildPreparedModelsProviderData } from "aether/plugin-sdk/models-provider-runtime";
export {
  GROUP_POLICY_BLOCKED_LABEL,
  resolveAllowlistProviderRuntimeGroupPolicy,
  resolveDefaultGroupPolicy,
  warnMissingProviderGroupPolicyFallbackOnce,
} from "aether/plugin-sdk/runtime-group-policy";
export { isDangerousNameMatchingEnabled } from "aether/plugin-sdk/dangerous-name-runtime";
export { resolveStorePath } from "aether/plugin-sdk/session-store-runtime";
export { formatInboundFromLabel } from "aether/plugin-sdk/channel-inbound";
export { logInboundDrop } from "aether/plugin-sdk/channel-inbound";
export { createChannelPairingController } from "aether/plugin-sdk/channel-pairing";
export { createChannelMessageReplyPipeline } from "aether/plugin-sdk/channel-outbound";
export { logTypingFailure } from "aether/plugin-sdk/channel-feedback";
export { loadOutboundMediaFromUrl } from "aether/plugin-sdk/outbound-media";
export { rawDataToString } from "aether/plugin-sdk/webhook-ingress";
export { chunkTextForOutbound } from "aether/plugin-sdk/text-chunking";
// Legacy map-helper exports stay for older plugin consumers. New message-turn
// code should use createChannelHistoryWindow.
export {
  DEFAULT_GROUP_HISTORY_LIMIT,
  createChannelHistoryWindow,
  buildPendingHistoryContextFromMap,
  clearHistoryEntriesIfEnabled,
  recordPendingHistoryEntryIfEnabled,
} from "aether/plugin-sdk/reply-history";
export { normalizeAccountId, resolveThreadSessionKeys } from "aether/plugin-sdk/routing";
export { resolveAllowlistMatchSimple } from "aether/plugin-sdk/allow-from";
export { registerPluginHttpRoute } from "aether/plugin-sdk/webhook-targets";
export {
  isRequestBodyLimitError,
  readRequestBodyWithLimit,
} from "aether/plugin-sdk/webhook-ingress";
export {
  applyAccountNameToChannelSection,
  applySetupAccountConfigPatch,
  migrateBaseNameToDefaultAccount,
} from "aether/plugin-sdk/setup";
export { resolveChannelMediaMaxBytes } from "aether/plugin-sdk/account-helpers";
export { getAgentScopedMediaLocalRoots } from "aether/plugin-sdk/media-runtime";
export { normalizeProviderId } from "aether/plugin-sdk/provider-model-shared";
export { setMattermostRuntime } from "./src/runtime.js";
