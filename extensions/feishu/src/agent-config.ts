// Feishu helper module supports agent config behavior.
import { resolveAgentConfig } from "aether/plugin-sdk/agent-scope-runtime";
import type { AetherConfig } from "../runtime-api.js";

type ReasoningDefault = "on" | "stream" | "off";

export function resolveFeishuConfigReasoningDefault(
  cfg: AetherConfig,
  agentId: string,
): ReasoningDefault {
  const agentDefault = resolveAgentConfig(cfg, agentId)?.reasoningDefault;
  return agentDefault ?? cfg.agents?.defaults?.reasoningDefault ?? "off";
}
