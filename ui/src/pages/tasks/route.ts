import { definePage } from "@openclaw/uirouter";
import { html } from "lit";
import { routePageSpec } from "../../app-route-paths.ts";

export const page = definePage({
  ...routePageSpec("tasks"),
  component: () =>
    import("./tasks-page.ts").then(() => ({
      header: true,
      render: () => html`<aether-tasks-page></aether-tasks-page>`,
    })),
});
