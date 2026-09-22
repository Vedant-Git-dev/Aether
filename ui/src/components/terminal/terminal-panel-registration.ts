import { AetherTerminalPanel } from "./terminal-panel.ts";

// Guarded define so shared registries can retain this module across reloads.
if (!customElements.get("aether-terminal-panel")) {
  customElements.define("aether-terminal-panel", AetherTerminalPanel);
}
