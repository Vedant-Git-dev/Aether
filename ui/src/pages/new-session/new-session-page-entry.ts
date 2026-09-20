import { html } from "lit";
import { NewSessionPage } from "./new-session-page.ts";

if (!customElements.get("aether-new-session-page")) {
  customElements.define("aether-new-session-page", NewSessionPage);
}

export const render = (data: unknown) =>
  html`<aether-new-session-page .data=${data}></aether-new-session-page>`;
