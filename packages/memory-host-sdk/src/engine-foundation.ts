// Real workspace contract for memory engine foundation concerns.

export {
  resolveAgentContextLimits,
  resolveAgentDir,
  resolveAgentWorkspaceDir,
  resolveDefaultAgentId,
  resolveSessionAgentId,
} from "./host/aether-runtime-agent.js";
export {
  resolveMemorySearchConfig,
  resolveMemorySearchSyncConfig,
  type ResolvedMemorySearchConfig,
  type ResolvedMemorySearchSyncConfig,
} from "./host/aether-runtime-agent.js";
export { parseDurationMs } from "./host/aether-runtime-config.js";
export { loadConfig } from "./host/aether-runtime-session.js";
export { resolveStateDir } from "./host/aether-runtime-config.js";
export { resolveSessionTranscriptsDirForAgent } from "./host/aether-runtime-config.js";
export {
  hasConfiguredSecretInput,
  normalizeResolvedSecretInputString,
} from "./host/aether-runtime-config.js";
export { root } from "./host/aether-runtime-io.js";
export { isPathInside } from "./host/fs-utils.js";
export { createSubsystemLogger } from "./host/aether-runtime-io.js";
export { detectMime } from "./host/aether-runtime-io.js";
export { resolveGlobalSingleton } from "./host/aether-runtime-io.js";
export { onSessionTranscriptUpdate } from "./host/aether-runtime-session.js";
export { splitShellArgs } from "./host/aether-runtime-io.js";
export { runTasksWithConcurrency } from "./host/aether-runtime-io.js";
export {
  shortenHomeInString,
  shortenHomePath,
  resolveUserPath,
  truncateUtf16Safe,
} from "./host/aether-runtime-io.js";
export type { AetherConfig } from "./host/aether-runtime-config.js";
export type { SecretInput } from "./host/aether-runtime-config.js";
export type { MemoryCitationsMode } from "./host/aether-runtime-config.js";
export type { MemorySearchConfig } from "./host/aether-runtime-config.js";
