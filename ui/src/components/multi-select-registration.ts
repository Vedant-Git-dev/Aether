import { MultiSelect } from "./multi-select.ts";

if (!customElements.get("aether-multi-select")) {
  customElements.define("aether-multi-select", MultiSelect);
}

declare global {
  interface HTMLElementTagNameMap {
    "aether-multi-select": MultiSelect;
  }
}
