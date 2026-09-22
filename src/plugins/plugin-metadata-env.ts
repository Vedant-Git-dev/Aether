import { tryProcessCwd } from "../infra/safe-cwd.js";
import { shouldTrustTestBundledPluginsDirOverride } from "./bundled-dir.js";
import {
  hasActivePluginInstallRoots,
  resolveActivePluginInstallRoots,
} from "./install-root-context.js";
import { hashJson } from "./installed-plugin-index-hash.js";

const PLUGIN_METADATA_ENV_KEYS = [
  "APPDATA",
  "HOME",
  "AETHER_BUNDLED_PLUGINS_DIR",
  "AETHER_COMPATIBILITY_HOST_VERSION",
  "AETHER_CONFIG_PATH",
  "AETHER_DEV_SOURCE_ROOT",
  "AETHER_DISABLE_BUNDLED_PLUGINS",
  "AETHER_DISABLE_BUNDLED_SOURCE_OVERLAYS",
  "AETHER_HOME",
  "AETHER_NIX_MODE",
  "AETHER_STATE_DIR",
  "PREFIX",
  "USERPROFILE",
  "XDG_CONFIG_HOME",
] as const;

/** Compares discovery namespaces without resolving or probing filesystem roots. */
export function resolvePluginMetadataEnvFingerprint(env: NodeJS.ProcessEnv = process.env): string {
  return hashJson({
    env: Object.fromEntries(
      PLUGIN_METADATA_ENV_KEYS.flatMap((key) => {
        const value = env[key];
        return value === undefined ? [] : [[key, value]];
      }),
    ),
    installRoots: hasActivePluginInstallRoots() ? resolveActivePluginInstallRoots() : undefined,
    trustBundledPluginsDirOverride: shouldTrustTestBundledPluginsDirOverride(env),
    cwd: tryProcessCwd(),
  });
}
