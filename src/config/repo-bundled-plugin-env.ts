import os from "node:os";
import path from "node:path";

// Validation never creates or writes this state dir: no operator DB is opened,
// and no operator-installed plugin can leak config keys into the checkout's audit.
export function resolveRepoBundledPluginEnv(bundledPluginsDir: string): NodeJS.ProcessEnv {
  return {
    ...process.env,
    AETHER_STATE_DIR: path.join(os.tmpdir(), "aether-repo-bundled-plugin-state"),
    AETHER_BUNDLED_PLUGINS_DIR: bundledPluginsDir,
  };
}
