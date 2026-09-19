// Whatsapp plugin module implements group gating behavior.
export {
  implicitMentionKindWhen,
  resolveInboundMentionDecision,
} from "aether/plugin-sdk/channel-mention-gating";
export { hasControlCommand } from "aether/plugin-sdk/command-detection";
export { createChannelHistoryWindow } from "aether/plugin-sdk/reply-history";
export { parseActivationCommand } from "aether/plugin-sdk/group-activation";
export { normalizeE164 } from "../../text-runtime.js";
