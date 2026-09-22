// Private runtime barrel for the bundled Google Chat extension.
// Keep this barrel thin and avoid broad plugin-sdk surfaces during bootstrap.

export { DEFAULT_ACCOUNT_ID } from "aether/plugin-sdk/account-id";
export {
  createActionGate,
  jsonResult,
  readNumberParam,
  readReactionParams,
  readStringParam,
} from "aether/plugin-sdk/channel-actions";
export { buildChannelConfigSchema, GoogleChatConfigSchema } from "./config-api.js";
export type {
  ChannelMessageActionAdapter,
  ChannelMessageActionName,
  ChannelStatusIssue,
} from "aether/plugin-sdk/channel-contract";
export { missingTargetError } from "aether/plugin-sdk/channel-feedback";
export {
  createAccountStatusSink,
  runPassiveAccountLifecycle,
} from "aether/plugin-sdk/channel-outbound";
export { createChannelPairingController } from "aether/plugin-sdk/channel-pairing";
export { createChannelMessageReplyPipeline } from "aether/plugin-sdk/channel-outbound";
export { PAIRING_APPROVED_MESSAGE } from "aether/plugin-sdk/channel-status";
export { chunkTextForOutbound } from "aether/plugin-sdk/text-chunking";
export type { AetherConfig } from "aether/plugin-sdk/config-contracts";
export {
  GROUP_POLICY_BLOCKED_LABEL,
  resolveAllowlistProviderRuntimeGroupPolicy,
  resolveDefaultGroupPolicy,
  warnMissingProviderGroupPolicyFallbackOnce,
} from "aether/plugin-sdk/runtime-group-policy";
export { isDangerousNameMatchingEnabled } from "aether/plugin-sdk/dangerous-name-runtime";
export type { PluginRuntime } from "aether/plugin-sdk/runtime-store";
export { fetchWithSsrFGuard } from "aether/plugin-sdk/ssrf-runtime";
export type {
  GoogleChatAccountConfig,
  GoogleChatConfig,
} from "aether/plugin-sdk/config-contracts";
export { extractToolSend } from "aether/plugin-sdk/tool-send";
export { resolveInboundMentionDecision } from "aether/plugin-sdk/channel-inbound";
export { resolveWebhookPath } from "aether/plugin-sdk/webhook-ingress";
export {
  registerWebhookTargetWithPluginRoute,
  resolveWebhookTargetWithAuthOrReject,
  withResolvedWebhookRequestPipeline,
} from "aether/plugin-sdk/webhook-targets";
export {
  createWebhookInFlightLimiter,
  readJsonWebhookBodyOrReject,
  type WebhookInFlightLimiter,
} from "aether/plugin-sdk/webhook-request-guards";
export { setGoogleChatRuntime } from "./src/runtime.js";
