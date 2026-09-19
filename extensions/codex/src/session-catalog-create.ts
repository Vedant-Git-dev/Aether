import { resolveDefaultAgentId } from "aether/plugin-sdk/agent-scope-runtime";
import type { AetherConfig } from "aether/plugin-sdk/config-contracts";
import type { PluginRuntime } from "aether/plugin-sdk/core";

const CODEX_AGENT_RUNTIME_ID = "codex";
const CODEX_CATALOG_DEFAULT_MODEL_REF = "openai/gpt-5.6-sol";

export function resolveCodexCatalogCreateSession(
  modelConfig: Pick<
    PluginRuntime["modelConfig"],
    "resolveAllowedModelRef" | "resolveDefaultModelForAgent"
  >,
  config: AetherConfig | undefined,
  requestedAgentId?: string,
): { model: string; agentRuntime: string } | undefined {
  if (!config) {
    return undefined;
  }
  const agentId = requestedAgentId ?? resolveDefaultAgentId(config);
  const defaultModel = modelConfig.resolveDefaultModelForAgent({ cfg: config, agentId });
  const allowed = modelConfig.resolveAllowedModelRef({
    cfg: config,
    catalog: [],
    raw: CODEX_CATALOG_DEFAULT_MODEL_REF,
    defaultProvider: defaultModel.provider,
    defaultModel: defaultModel.model,
    agentId,
  });
  return "error" in allowed
    ? undefined
    : { model: CODEX_CATALOG_DEFAULT_MODEL_REF, agentRuntime: CODEX_AGENT_RUNTIME_ID };
}
