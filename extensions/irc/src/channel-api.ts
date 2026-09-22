// Irc API module exposes the plugin public contract.
export { createAccountStatusSink } from "aether/plugin-sdk/channel-outbound";
export { DEFAULT_ACCOUNT_ID } from "aether/plugin-sdk/account-id";
export type { ChannelPlugin } from "aether/plugin-sdk/channel-core";
export { PAIRING_APPROVED_MESSAGE } from "aether/plugin-sdk/channel-status";
export { buildBaseChannelStatusSummary } from "aether/plugin-sdk/status-helpers";
export { chunkTextForOutbound } from "aether/plugin-sdk/text-chunking";
