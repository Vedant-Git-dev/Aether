import { getSessionEntry } from "aether/plugin-sdk/session-store-runtime";
import type { AetherConfig } from "../runtime-api.js";
// Feishu plugin module implements reasoning preview behavior.
import { resolveFeishuConfigReasoningDefault } from "./agent-config.js";

export function resolveFeishuReasoningPreviewEnabled(params: {
  cfg: AetherConfig;
  agentId: string;
  storePath: string;
  sessionKey?: string;
}): boolean {
  const configDefault = resolveFeishuConfigReasoningDefault(params.cfg, params.agentId);

  if (!params.sessionKey) {
    return configDefault === "stream";
  }

  try {
    const level = getSessionEntry({
      storePath: params.storePath,
      sessionKey: params.sessionKey,
      readConsistency: "latest",
    })?.reasoningLevel;
    if (level === "on" || level === "stream" || level === "off") {
      return level === "stream";
    }
  } catch {
    return false;
  }
  return configDefault === "stream";
}
