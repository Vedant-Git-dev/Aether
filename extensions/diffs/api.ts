// Diffs API module exposes the plugin public contract.
export type { AetherConfig } from "aether/plugin-sdk/config-contracts";
export {
  definePluginEntry,
  type AnyAgentTool,
  type AetherPluginApi,
  type AetherPluginConfigSchema,
  type AetherPluginToolContext,
  type PluginLogger,
} from "aether/plugin-sdk/plugin-entry";
export { resolvePreferredAetherTmpDir } from "aether/plugin-sdk/temp-path";
