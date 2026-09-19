// Mattermost API module exposes the plugin public contract.
export type {
  BaseProbeResult,
  ChannelAccountSnapshot,
  ChannelDirectoryEntry,
  ChatType,
  HistoryEntry,
  AetherConfig,
  AetherPluginApi,
  ReplyPayload,
} from "aether/plugin-sdk/core";
export type { RuntimeEnv } from "aether/plugin-sdk/runtime";
export { resolveAllowlistMatchSimple } from "aether/plugin-sdk/allow-from";
export { logInboundDrop } from "aether/plugin-sdk/channel-inbound";
export { createChannelPairingController } from "aether/plugin-sdk/channel-pairing";
export { createChannelMessageReplyPipeline } from "aether/plugin-sdk/channel-outbound";
export { logTypingFailure } from "aether/plugin-sdk/channel-feedback";
export { listSkillCommandsForAgents } from "aether/plugin-sdk/command-auth-native";
export { buildPreparedModelsProviderData } from "aether/plugin-sdk/models-provider-runtime";
export { isDangerousNameMatchingEnabled } from "aether/plugin-sdk/dangerous-name-runtime";
export {
  resolveAllowlistProviderRuntimeGroupPolicy,
  resolveDefaultGroupPolicy,
  warnMissingProviderGroupPolicyFallbackOnce,
} from "aether/plugin-sdk/runtime-group-policy";
export { resolveChannelMediaMaxBytes } from "aether/plugin-sdk/account-helpers";
export { loadOutboundMediaFromUrl } from "aether/plugin-sdk/outbound-media";
// Legacy map-helper exports stay for older plugin consumers. New message-turn
// code should use createChannelHistoryWindow.
export {
  DEFAULT_GROUP_HISTORY_LIMIT,
  createChannelHistoryWindow,
} from "aether/plugin-sdk/reply-history";
export { registerPluginHttpRoute } from "aether/plugin-sdk/webhook-targets";
export { isRequestBodyLimitError } from "aether/plugin-sdk/webhook-ingress";
export {
  readRequestBodyWithLimit,
  sendHttpRequestRejection,
} from "aether/plugin-sdk/webhook-request-guards";
export { isTrustedProxyAddress, resolveClientIp } from "aether/plugin-sdk/core";
