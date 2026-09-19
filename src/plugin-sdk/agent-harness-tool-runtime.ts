/**
 * Focused runtime SDK subpath for native harness tool-surface routing.
 *
 * Keep tool-search and code-mode dependencies out of the lightweight harness
 * lifecycle facade used during plugin startup.
 */
import {
  createAgentHarnessToolSurfaceRuntimeCore,
  type AgentHarnessToolSurfaceRuntime as CoreAgentHarnessToolSurfaceRuntime,
} from "../agents/harness/tool-surface-bridge.js";

export { getCoreTtsToolResultMediaUrls } from "../agents/tools/tts-tool-result-provenance.js";
export { consumeTrustedToolNoStartError } from "../agents/tool-result-error.js";
export {
  acknowledgeInternalToolResult,
  copyInternalToolResultState,
} from "../agents/runtime/internal-hooks.js";

type AetherCodingToolsOptions = NonNullable<
  Parameters<typeof import("./agent-harness.js").createAetherCodingTools>[0]
>;

export type AgentHarnessToolSurfaceRuntime = Omit<
  CoreAgentHarnessToolSurfaceRuntime,
  "toolSearchCatalogExecutor" | "toolSearchCatalogRef"
> & {
  toolSearchCatalogExecutor: AetherCodingToolsOptions["toolSearchCatalogExecutor"];
  toolSearchCatalogRef: AetherCodingToolsOptions["toolSearchCatalogRef"];
};

export type AgentHarnessToolSurfaceRuntimeParams = Omit<
  Parameters<typeof createAgentHarnessToolSurfaceRuntimeCore>[0],
  "executeTool"
> & {
  executeTool: NonNullable<AetherCodingToolsOptions["toolSearchCatalogExecutor"]>;
};

export function createAgentHarnessToolSurfaceRuntime(
  params: AgentHarnessToolSurfaceRuntimeParams,
): AgentHarnessToolSurfaceRuntime {
  return createAgentHarnessToolSurfaceRuntimeCore(params);
}
