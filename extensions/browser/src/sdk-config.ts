/**
 * Browser-local SDK config bridge.
 */
export {
  getRuntimeConfig,
  getRuntimeConfigSourceSnapshot,
} from "aether/plugin-sdk/runtime-config-snapshot";
export { mutateConfigFile } from "aether/plugin-sdk/config-mutation";
export type { BrowserProfileConfig, AetherConfig } from "aether/plugin-sdk/config-contracts";
export {
  normalizePluginsConfig,
  resolveEffectiveEnableState,
} from "aether/plugin-sdk/plugin-config-runtime";
export {
  CONFIG_DIR,
  escapeRegExp,
  resolveUserPath,
} from "aether/plugin-sdk/text-utility-runtime";
