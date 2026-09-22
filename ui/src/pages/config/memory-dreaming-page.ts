// Dreams tab host. Agent selection is owned by the parent Memory page.
import { html, nothing } from "lit";
import { property } from "lit/decorators.js";
import { AetherLightDomElement } from "../../lit/aether-element.ts";
import "../agents/memory/memory-panel.ts";

class MemoryDreamingSettings extends AetherLightDomElement {
  @property() agentId: string | null = null;

  override render() {
    return html`
      ${
        this.agentId
          ? html`<aether-agent-memory-panel
              .agentId=${this.agentId}
            ></aether-agent-memory-panel>`
          : nothing
      }
    `;
  }
}

if (!customElements.get("aether-memory-dreaming")) {
  customElements.define("aether-memory-dreaming", MemoryDreamingSettings);
}
