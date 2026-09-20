import { html } from "lit";
import { property } from "lit/decorators.js";
import type { ControlUiEnvironment } from "../../../src/gateway/control-ui-bootstrap-contract.js";
import { beginNativeWindowDrag } from "../app/native-window-drag.ts";
import { controlUiPublicAssetPath } from "../app/public-assets.ts";
import { t } from "../i18n/index.ts";
import { AetherLightDomContentsElement } from "../lit/aether-element.ts";
import { icons } from "./icons.ts";
import "./tooltip.ts";

/** Narrow-viewport header: drawer toggle, brand, and command-palette search.
 * Desktop hides it entirely (layout.css) — the sidebar owns navigation there. */
class AppTopbar extends AetherLightDomContentsElement {
  @property({ attribute: false }) navDrawerOpen = false;
  @property({ attribute: false }) resourceBasePath = "";
  @property({ attribute: false }) environment: ControlUiEnvironment | null = null;
  @property({ attribute: false }) onToggleDrawer!: (trigger: HTMLElement) => void;
  @property({ attribute: false }) onOpenPalette!: () => void;

  override render() {
    const drawerLabel = this.navDrawerOpen ? t("nav.collapse") : t("nav.expand");
    // The brand row asks the Mac host to drag the window, replacing its native drag strip.
    return html`
      <header class="topbar">
        <div class="topnav-shell">
          <aether-tooltip .content=${drawerLabel}>
            <button
              type="button"
              class="topbar-icon-btn topbar-nav-toggle"
              @click=${(event: MouseEvent) =>
                this.onToggleDrawer(event.currentTarget as HTMLElement)}
              aria-label=${drawerLabel}
              aria-expanded=${String(this.navDrawerOpen)}
            >
              <span class="nav-collapse-toggle__icon" aria-hidden="true">${icons.menu}</span>
            </button>
          </aether-tooltip>
          <div class="topnav-shell__content" @mousedown=${beginNativeWindowDrag}>
            <div class="topbar-brand" aria-label="Aether">
              <img
                class="topbar-brand__logo"
                src=${controlUiPublicAssetPath("apple-touch-icon.png", this.resourceBasePath)}
                alt=""
                aria-hidden="true"
              />
              <span class="topbar-brand__title">Aether</span>
              ${
                this.environment &&
                html`<span class="control-ui-environment-pill">${this.environment.label}</span>`
              }
            </div>
          </div>
          <div class="topnav-shell__actions">
            <aether-tooltip .content=${t("chat.commandPaletteTitle")}>
              <button
                class="topbar-search"
                @click=${this.onOpenPalette}
                aria-label=${t("chat.openCommandPalette")}
              >
                ${icons.search}
              </button>
            </aether-tooltip>
          </div>
        </div>
      </header>
    `;
  }
}

if (!customElements.get("aether-app-topbar")) {
  customElements.define("aether-app-topbar", AppTopbar);
}
