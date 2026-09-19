// Whatsapp plugin module implements channel react action behavior.
import { readStringOrNumberParam, readStringParam } from "aether/plugin-sdk/channel-actions";
import type { AetherConfig } from "aether/plugin-sdk/config-contracts";

export { resolveReactionMessageId } from "aether/plugin-sdk/channel-actions";
export { handleWhatsAppAction } from "./action-runtime.js";
export { resolveAuthorizedWhatsAppOutboundTarget } from "./action-runtime-target-auth.js";
export { resolveWhatsAppAccount, resolveWhatsAppMediaMaxBytes } from "./accounts.js";
export { isWhatsAppGroupJid, normalizeWhatsAppTarget } from "./normalize.js";
export { sendWhatsAppUploadFile as sendMessageWhatsApp } from "./send.js";
export { readStringOrNumberParam, readStringParam, type AetherConfig };
