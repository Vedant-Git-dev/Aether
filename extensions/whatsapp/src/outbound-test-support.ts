// Whatsapp plugin module implements outbound test support behavior.
import type { AetherConfig } from "aether/plugin-sdk/config-contracts";

export function createWhatsAppPollFixture() {
  const cfg = { marker: "resolved-cfg" } as AetherConfig;
  const poll = {
    question: "Lunch?",
    options: ["Pizza", "Sushi"],
    maxSelections: 1,
  };
  return {
    cfg,
    poll,
    to: "+1555",
    accountId: "work",
  };
}
