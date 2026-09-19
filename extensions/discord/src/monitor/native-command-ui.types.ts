// Discord type declarations define plugin contracts.
import type { AetherConfig } from "aether/plugin-sdk/config-contracts";
import type { DiscordLivePolicyReader } from "./live-policy.js";
import type { DiscordDispatchReplyFromConfig } from "./native-command.types.js";
import type { ThreadBindingManager } from "./thread-bindings.js";

type DiscordConfig = NonNullable<AetherConfig["channels"]>["discord"];

export type DiscordCommandArgContext = {
  readPolicy?: DiscordLivePolicyReader;
  cfg: AetherConfig;
  discordConfig: DiscordConfig;
  accountId: string;
  sessionPrefix: string;
  threadBindings: ThreadBindingManager;
  dispatchReplyFromConfig?: DiscordDispatchReplyFromConfig;
  postApplySettleMs?: number;
};

export type DiscordModelPickerContext = DiscordCommandArgContext;

export type SafeDiscordInteractionCall = <T>(
  label: string,
  fn: () => Promise<T>,
) => Promise<T | null>;
