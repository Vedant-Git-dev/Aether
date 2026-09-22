import { consume } from "@lit/context";
import { asOptionalRecord } from "@aether/normalization-core/record-coerce";
import { html } from "lit";
import { titleForRoute } from "../../app-navigation.ts";
import type { RouteId } from "../../app-route-paths.ts";
import { applicationContext, type ApplicationContext } from "../../app/context.ts";
import { isNativeWebChromeHost } from "../../app/native-web-chrome.ts";
import { renderSettingsWorkspace } from "../../components/settings-workspace.ts";
import { AetherLightDomElement } from "../../lit/aether-element.ts";
import { SubscriptionsController } from "../../lit/subscriptions-controller.ts";
import { buildMacGatewayLaunchUrl } from "./gateway-launch.ts";
import { renderApps } from "./view.ts";

class AppsPage extends AetherLightDomElement {
  @consume({ context: applicationContext, subscribe: true })
  private context!: ApplicationContext;

  // Re-render on gateway snapshots so the launch URL follows connection state.
  private readonly subscriptions = new SubscriptionsController(this).watch(
    () => this.context?.gateway,
    (gateway, notify) => gateway.subscribe(notify),
  );

  override disconnectedCallback() {
    this.subscriptions.clear();
    super.disconnectedCallback();
  }

  override render() {
    const gatewaySnapshot = this.context.gateway.snapshot;
    const body = renderApps({
      onNavigate: (routeId: RouteId) => this.context.navigate(routeId),
      macGatewayLaunchUrl:
        gatewaySnapshot.phase === "connected" && !isNativeWebChromeHost()
          ? buildMacGatewayLaunchUrl(
              this.context.gateway.connection.gatewayUrl,
              asOptionalRecord(gatewaySnapshot.hello?.snapshot)?.controlUiIdentityUrl,
            )
          : null,
    });
    return html`
      <section class="content-header">
        <div>
          <div class="page-title">${titleForRoute("apps")}</div>
        </div>
      </section>
      ${renderSettingsWorkspace(body)}
    `;
  }
}

if (!customElements.get("aether-apps-page")) {
  customElements.define("aether-apps-page", AppsPage);
}
