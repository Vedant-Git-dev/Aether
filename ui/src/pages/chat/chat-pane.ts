// Public custom-element entrypoint for the Control UI chat pane.
import { ChatPane } from "./chat-pane-render.ts";

if (!customElements.get("aether-chat-pane")) {
  customElements.define("aether-chat-pane", ChatPane);
}

declare global {
  interface HTMLElementTagNameMap {
    "aether-chat-pane": ChatPane;
  }
}
