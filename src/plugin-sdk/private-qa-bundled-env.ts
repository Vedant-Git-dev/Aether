/**
 * Runtime helper for private QA CLI source-checkout bundled plugin resolution.
 */
import fs from "node:fs";
import path from "node:path";
import { resolveAetherPackageRootSync } from "../infra/aether-root.js";

/** Returns an env override that points bundled plugin loading at source extensions. */
export function resolvePrivateQaBundledPluginsEnv(
  env: NodeJS.ProcessEnv = process.env,
): NodeJS.ProcessEnv | undefined {
  if (env.AETHER_ENABLE_PRIVATE_QA_CLI !== "1") {
    return undefined;
  }
  const packageRoot = resolveAetherPackageRootSync({
    argv1: process.argv[1],
    cwd: process.cwd(),
    moduleUrl: import.meta.url,
  });
  if (!packageRoot) {
    return undefined;
  }
  const sourceExtensionsDir = path.join(packageRoot, "extensions");
  if (
    !fs.existsSync(path.join(packageRoot, ".git")) ||
    !fs.existsSync(path.join(packageRoot, "src")) ||
    !fs.existsSync(sourceExtensionsDir)
  ) {
    return undefined;
  }
  return {
    ...env,
    AETHER_BUNDLED_PLUGINS_DIR: sourceExtensionsDir,
  };
}
