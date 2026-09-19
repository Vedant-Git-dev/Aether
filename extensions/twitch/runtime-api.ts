// Private runtime barrel for the bundled Twitch extension.
// Keep this barrel thin and aligned with the local extension surface.

export type {
  ChannelAccountSnapshot,
  ChannelCapabilities,
  ChannelGatewayContext,
  ChannelLogSink,
  ChannelMessageActionAdapter,
  ChannelMessageActionContext,
  ChannelMeta,
  ChannelOutboundAdapter,
  ChannelOutboundContext,
  ChannelResolveKind,
  ChannelResolveResult,
  ChannelStatusAdapter,
} from "aether/plugin-sdk/channel-contract";
export type { ChannelPlugin } from "aether/plugin-sdk/channel-core";
export type { OutboundDeliveryResult } from "aether/plugin-sdk/channel-send-result";
export type { AetherConfig } from "aether/plugin-sdk/config-contracts";
export type { RuntimeEnv } from "aether/plugin-sdk/runtime";
export type { WizardPrompter } from "aether/plugin-sdk/setup";
