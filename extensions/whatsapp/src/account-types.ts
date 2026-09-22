// Whatsapp plugin module implements account types behavior.
import type { AetherConfig } from "aether/plugin-sdk/config-contracts";

export type WhatsAppAccountConfig = NonNullable<
  NonNullable<NonNullable<AetherConfig["channels"]>["whatsapp"]>["accounts"]
>[string];
