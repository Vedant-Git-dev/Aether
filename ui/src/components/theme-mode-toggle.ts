import { html } from "lit";
import { property } from "lit/decorators.js";
import type { ThemeMode } from "../app/theme.ts";
import { t } from "../i18n/index.ts";
import { AetherLightDomContentsElement } from "../lit/aether-element.ts";
import { icons } from "./icons.ts";
import "./tooltip.ts";

export type ThemeModeChangeDetail = {
  mode: ThemeMode;
  element: HTMLElement;
};

class ThemeModeToggle extends AetherLightDomContentsElement {
  @property({ attribute: false }) mode: ThemeMode = "system";

  private readonly handleModeChange = (event: Event) => {
    const mode = this.mode === "system" ? "light" : this.mode === "light" ? "dark" : "system";
    this.dispatchEvent(
      new CustomEvent<ThemeModeChangeDetail>("theme-change", {
        detail: { mode, element: event.currentTarget as HTMLElement },
        bubbles: true,
        composed: true,
      }),
    );
  };

  override render() {
    const labelKey =
      this.mode === "system"
        ? "common.system"
        : this.mode === "light"
          ? "common.light"
          : "common.dark";
    const label = t(labelKey);
    const tooltip = t("common.colorModeOption", { mode: label });

    return html`
      <aether-tooltip .content=${tooltip}>
        <button
          type="button"
          class="theme-mode-toggle"
          aria-label=${tooltip}
          @click=${this.handleModeChange}
        >
          ${this.mode === "system" ? icons.monitor : this.mode === "light" ? icons.sun : icons.moon}
        </button>
      </aether-tooltip>
    `;
  }
}

if (!customElements.get("aether-theme-mode-toggle")) {
  customElements.define("aether-theme-mode-toggle", ThemeModeToggle);
}
