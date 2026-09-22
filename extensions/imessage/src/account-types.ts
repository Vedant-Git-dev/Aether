// Imessage plugin module implements account types behavior.
import type { AetherConfig } from "aether/plugin-sdk/config-contracts";

export type IMessageAccountConfig = Omit<
  NonNullable<NonNullable<AetherConfig["channels"]>["imessage"]>,
  "accounts" | "defaultAccount"
>;
