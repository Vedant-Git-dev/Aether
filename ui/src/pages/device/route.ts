import { definePage } from "@openclaw/uirouter";
import { html } from "lit";
import { routePageSpec } from "../../app-route-paths.ts";

export const page = definePage({
  ...routePageSpec("device"),
  component: () =>
    import("./device-page.ts").then(() => ({
      header: true,
      render: () => html`<aether-device-page></aether-device-page>`,
    })),
});

export const permissionsPage = definePage({
  ...routePageSpec("device-permissions"),
  component: () =>
    import("./permissions-page.ts").then(() => ({
      header: true,
      render: () => html`<aether-device-permissions-page></aether-device-permissions-page>`,
    })),
});
