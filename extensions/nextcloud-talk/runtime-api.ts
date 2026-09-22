// Private runtime barrel for the bundled Nextcloud Talk extension.
// Keep this barrel thin and aligned with the local extension surface.

export type { AllowlistMatch } from "aether/plugin-sdk/allow-from";
export type { ChannelGroupContext } from "aether/plugin-sdk/channel-contract";
export { logInboundDrop } from "aether/plugin-sdk/channel-inbound";
export { createChannelPairingController } from "aether/plugin-sdk/channel-pairing";
export type {
  BlockStreamingCoalesceConfig,
  DmConfig,
  DmPolicy,
  GroupPolicy,
  GroupToolPolicyConfig,
  AetherConfig,
} from "aether/plugin-sdk/config-contracts";
export {
  GROUP_POLICY_BLOCKED_LABEL,
  resolveAllowlistProviderRuntimeGroupPolicy,
  resolveDefaultGroupPolicy,
  warnMissingProviderGroupPolicyFallbackOnce,
} from "aether/plugin-sdk/runtime-group-policy";
export { createChannelMessageReplyPipeline } from "aether/plugin-sdk/channel-outbound";
export type { OutboundReplyPayload } from "aether/plugin-sdk/reply-payload";
export { deliverFormattedTextWithAttachments } from "aether/plugin-sdk/reply-payload";
export type { PluginRuntime } from "aether/plugin-sdk/runtime-store";
export type { RuntimeEnv } from "aether/plugin-sdk/runtime";
export type { SecretInput } from "aether/plugin-sdk/secret-input";
export { fetchWithSsrFGuard } from "aether/plugin-sdk/ssrf-runtime";
export { setNextcloudTalkRuntime } from "./src/runtime.js";
