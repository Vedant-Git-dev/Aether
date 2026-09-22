import type { AetherConfig } from "../../config/types.aether.js";
import { tryResolveSessionCompatibilityOwnerAgentId } from "../session-request-agent.js";

export function resolveChatSendStopOwnerScope(params: {
  cfg: AetherConfig;
  selectedAgentId?: string;
  sessionKey: string;
}): { agentId?: string; defaultAgentId?: string } {
  return {
    agentId: params.selectedAgentId,
    defaultAgentId: tryResolveSessionCompatibilityOwnerAgentId(params.cfg, params.sessionKey),
  };
}
