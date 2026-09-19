// Mattermost API module exposes the plugin public contract.
export { createAccountStatusSink } from "aether/plugin-sdk/channel-outbound";
export type { ChannelPlugin } from "aether/plugin-sdk/core";
export { DEFAULT_ACCOUNT_ID } from "aether/plugin-sdk/core";
export { chunkTextForOutbound } from "aether/plugin-sdk/text-chunking";
