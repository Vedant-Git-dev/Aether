// Matrix plugin module implements monitor route test support behavior.
export {
  registerSessionBindingAdapter,
  testing,
} from "aether/plugin-sdk/session-binding-runtime";
export { resolveAgentRoute } from "aether/plugin-sdk/routing";
export {
  createTestRegistry,
  setActivePluginRegistry,
} from "aether/plugin-sdk/plugin-test-runtime";
export type { AetherConfig } from "aether/plugin-sdk/config-contracts";
