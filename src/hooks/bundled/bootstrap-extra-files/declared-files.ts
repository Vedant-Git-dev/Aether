// The bundled handler and diagnostics read the same declared extra files.
import { normalizeTrimmedStringList } from "@aether/normalization-core/string-normalization";
import { loadExtraBootstrapFilesWithDiagnostics } from "../../../agents/workspace.js";
import type { AetherConfig } from "../../../config/types.aether.js";
import { resolveHookConfig } from "../../config.js";

const HOOK_KEY = "bootstrap-extra-files";

/** Resolve legacy and current config keys for extra bootstrap file patterns. */
function resolveExtraBootstrapPatterns(cfg: AetherConfig | undefined): string[] {
  const hookConfig = resolveHookConfig(cfg, HOOK_KEY);
  if (!hookConfig || hookConfig.enabled === false) {
    return [];
  }
  const fromPaths = normalizeTrimmedStringList(hookConfig.paths);
  if (fromPaths.length > 0) {
    return fromPaths;
  }
  const fromPatterns = normalizeTrimmedStringList(hookConfig.patterns);
  if (fromPatterns.length > 0) {
    return fromPatterns;
  }
  return normalizeTrimmedStringList(hookConfig.files);
}

/** Loads the extra bootstrap files the hook config declares for a workspace. */
export async function loadDeclaredExtraBootstrapFiles(params: {
  config: AetherConfig | undefined;
  workspaceDir: string;
}): ReturnType<typeof loadExtraBootstrapFilesWithDiagnostics> {
  const patterns = resolveExtraBootstrapPatterns(params.config);
  if (patterns.length === 0) {
    return { files: [], diagnostics: [] };
  }
  return loadExtraBootstrapFilesWithDiagnostics(params.workspaceDir, patterns);
}
