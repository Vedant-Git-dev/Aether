// Focused runtime contract for memory plugin config/state/helpers.

export type { AnyAgentTool } from "./host/aether-runtime-agent.js";
export { resolveCronStyleNow } from "./host/aether-runtime-agent.js";
export { DEFAULT_AGENT_COMPACTION_RESERVE_TOKENS_FLOOR } from "./host/aether-runtime-agent.js";
export { resolveDefaultAgentId, resolveSessionAgentId } from "./host/aether-runtime-agent.js";
export { resolveMemorySearchConfig } from "./host/aether-runtime-agent.js";
export {
  asToolParamsRecord,
  jsonResult,
  readNumberParam,
  readStringParam,
} from "./host/aether-runtime-agent.js";
export { SILENT_REPLY_TOKEN } from "./host/aether-runtime-session.js";
export { parseNonNegativeByteSize } from "./host/aether-runtime-config.js";
export {
  getRuntimeConfig,
  /** @deprecated Use getRuntimeConfig(), or pass the already loaded config through the call path. */
  loadConfig,
} from "./host/aether-runtime-session.js";
export { resolveStateDir } from "./host/aether-runtime-config.js";
export { resolveSessionTranscriptsDirForAgent } from "./host/aether-runtime-config.js";
export { emptyPluginConfigSchema } from "./host/aether-runtime-memory.js";
export {
  buildActiveMemoryPromptSection,
  getMemoryCapabilityRegistration,
  listActiveMemoryPublicArtifacts,
} from "./host/aether-runtime-memory.js";
export { parseAgentSessionKey } from "./host/aether-runtime-agent.js";
export type { AetherConfig } from "./host/aether-runtime-config.js";
export type { MemoryCitationsMode } from "./host/aether-runtime-config.js";
export type {
  MemoryFlushPlan,
  MemoryFlushPlanResolver,
  MemoryPluginCapability,
  MemoryPluginPublicArtifact,
  MemoryPluginPublicArtifactsProvider,
  MemoryPluginRuntime,
  MemoryPromptSectionBuilder,
} from "./host/aether-runtime-memory.js";
export type { AetherPluginApi } from "./host/aether-runtime-memory.js";
