// Discord type declarations define plugin contracts.
import type { ChannelInboundTurnPlan } from "aether/plugin-sdk/channel-inbound";
import type { AetherConfig } from "aether/plugin-sdk/config-contracts";
import type { CommandArgValues } from "aether/plugin-sdk/native-command-registry";

export type DiscordConfig = NonNullable<AetherConfig["channels"]>["discord"];
export type DiscordDispatchReplyFromConfig = NonNullable<
  ChannelInboundTurnPlan["dispatchReplyFromConfig"]
>;

export type DiscordCommandArgs = {
  raw?: string;
  values?: CommandArgValues;
};
