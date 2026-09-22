// WhatsApp agent tool facade keeps the bundled entrypoint light during discovery.
import type { AetherPluginApi } from "aether/plugin-sdk/core";
import { registerWhatsAppCallTool } from "./src/agent-tools-call.js";
import { registerWhatsAppLoginTool } from "./src/agent-tools-login.js";
import { registerWhatsAppRosterTools } from "./src/agent-tools-roster.js";

export function registerWhatsAppAgentTools(api: AetherPluginApi): void {
  registerWhatsAppCallTool(api);
  registerWhatsAppLoginTool(api);
  registerWhatsAppRosterTools(api);
}
