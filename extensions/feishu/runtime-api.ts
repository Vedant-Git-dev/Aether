// Private runtime barrel for the bundled Feishu extension.
// Keep this barrel thin and generic-only.

export type {
  AllowlistMatch,
  AnyAgentTool,
  BaseProbeResult,
  ChannelGroupContext,
  ChannelMessageActionName,
  ChannelMeta,
  ChannelOutboundAdapter,
  ChannelPlugin,
  HistoryEntry,
  AetherConfig,
  AetherPluginApi,
  OutboundIdentity,
  PluginRuntime,
  ReplyPayload,
} from "aether/plugin-sdk/core";
export type { AetherConfig as AetherConfig } from "aether/plugin-sdk/core";
export type RuntimeEnv = {
  log: (...args: unknown[]) => void;
  error: (...args: unknown[]) => void;
  exit: (code: number) => void;
};
export type { GroupToolPolicyConfig } from "aether/plugin-sdk/config-contracts";
export {
  DEFAULT_ACCOUNT_ID,
  buildChannelConfigSchema,
  createActionGate,
  createDedupeCache,
} from "aether/plugin-sdk/core";
export {
  PAIRING_APPROVED_MESSAGE,
  buildProbeChannelStatusSummary,
  createDefaultChannelRuntimeState,
} from "aether/plugin-sdk/channel-status";
export { createChannelPairingController } from "aether/plugin-sdk/channel-pairing";
export { createReplyPrefixContext } from "aether/plugin-sdk/channel-outbound";
export {
  evaluateSupplementalContextVisibility,
  filterSupplementalContextItems,
  resolveChannelContextVisibilityMode,
} from "aether/plugin-sdk/context-visibility-runtime";
export { getSessionEntry } from "aether/plugin-sdk/session-store-runtime";
export { readJsonFileWithFallback } from "aether/plugin-sdk/json-store";
export { normalizeAgentId } from "aether/plugin-sdk/routing";
export { chunkTextForOutbound } from "aether/plugin-sdk/text-chunking";
export {
  isRequestBodyLimitError,
  readRequestBodyWithLimit,
  requestBodyErrorToText,
} from "aether/plugin-sdk/webhook-ingress";
export { setFeishuRuntime } from "./src/runtime.js";
