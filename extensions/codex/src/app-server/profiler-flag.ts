/**
 * Resolves whether Codex app-server profiling instrumentation is enabled by
 * Aether diagnostic flags.
 */
import type { AetherConfig } from "aether/plugin-sdk/config-contracts";
import { isDiagnosticFlagEnabled } from "aether/plugin-sdk/diagnostic-flags";

const PROFILER_FLAGS = ["profiler", "codex.profiler"] as const;

/** Checks the generic and Codex-specific profiler diagnostic flags. */
export function isCodexAppServerProfilerEnabled(
  config?: AetherConfig,
  env: NodeJS.ProcessEnv = process.env,
): boolean {
  return PROFILER_FLAGS.some((flag) => isDiagnosticFlagEnabled(flag, config, env));
}
