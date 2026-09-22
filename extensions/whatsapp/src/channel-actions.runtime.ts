// Whatsapp plugin module implements channel actions behavior.
import { createActionGate } from "aether/plugin-sdk/channel-actions";
import type { ChannelMessageActionName } from "aether/plugin-sdk/channel-contract";
import type { AetherConfig } from "aether/plugin-sdk/config-contracts";

export { listWhatsAppAccountIds, resolveWhatsAppAccount } from "./accounts.js";
export { resolveWhatsAppReactionLevel } from "./reaction-level.js";
export { createActionGate, type ChannelMessageActionName, type AetherConfig };
