import { ApprovalPage } from "./approval-page.ts";

if (!customElements.get("aether-approval-page")) {
  customElements.define("aether-approval-page", ApprovalPage);
}
