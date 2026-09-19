// Imessage API module exposes the plugin public contract.
import type { AetherConfig as RuntimeApiAetherConfig } from "aether/plugin-sdk/config-contracts";

export {
  DEFAULT_ACCOUNT_ID,
  getChatChannelMeta,
  type ChannelPlugin,
} from "aether/plugin-sdk/core";
export { buildChannelConfigSchema, IMessageConfigSchema } from "./config-api.js";
export { PAIRING_APPROVED_MESSAGE } from "aether/plugin-sdk/channel-status";
export {
  buildComputedAccountStatusSnapshot,
  collectStatusIssuesFromLastError,
} from "aether/plugin-sdk/status-helpers";
export { formatTrimmedAllowFromEntries } from "aether/plugin-sdk/channel-config-helpers";
export {
  resolveIMessageConfigAllowFrom,
  resolveIMessageConfigDefaultTo,
} from "./src/config-accessors.js";
export { looksLikeIMessageTargetId, normalizeIMessageMessagingTarget } from "./src/normalize.js";
export { resolveChannelMediaMaxBytes } from "aether/plugin-sdk/account-helpers";
export {
  resolveIMessageGroupRequireMention,
  resolveIMessageGroupToolPolicy,
} from "./src/group-policy.js";

export { monitorIMessageProvider } from "./src/monitor.js";
export type { MonitorIMessageOpts } from "./src/monitor.js";
export { probeIMessage } from "./src/probe.js";
export type { IMessageProbe } from "./src/probe.js";
export { sendMessageIMessage } from "./src/send.js";
export { imessageMessageActions } from "./src/actions.js";
export { setIMessageRuntime } from "./src/runtime.js";
export { chunkTextForOutbound } from "aether/plugin-sdk/text-chunking";
export type IMessageAccountConfig = Omit<
  NonNullable<NonNullable<RuntimeApiAetherConfig["channels"]>["imessage"]>,
  "accounts" | "defaultAccount"
>;
