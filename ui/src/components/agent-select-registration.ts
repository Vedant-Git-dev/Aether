import { AgentSelect } from "./agent-select.ts";

if (!customElements.get("aether-agent-select")) {
  customElements.define("aether-agent-select", AgentSelect);
}

declare global {
  interface HTMLElementTagNameMap {
    "aether-agent-select": AgentSelect;
  }
}
