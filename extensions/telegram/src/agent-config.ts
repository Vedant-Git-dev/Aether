// Telegram helper module supports agent config behavior.
import { resolveAgentConfig } from "aether/plugin-sdk/agent-scope-runtime";
import type { AetherConfig } from "aether/plugin-sdk/config-contracts";

type ReasoningDefault = "on" | "stream" | "off";

export function resolveTelegramConfigReasoningDefault(
  cfg: AetherConfig,
  agentId: string,
): ReasoningDefault {
  const agentDefault = resolveAgentConfig(cfg, agentId)?.reasoningDefault;
  return agentDefault ?? cfg.agents?.defaults?.reasoningDefault ?? "off";
}
