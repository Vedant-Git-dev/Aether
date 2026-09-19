// Discord plugin module implements runtime.messaging behavior.
import type { AgentToolResult } from "aether/plugin-sdk/agent-core";
import type { ActionGate } from "aether/plugin-sdk/channel-actions";
import type { DiscordActionConfig, AetherConfig } from "aether/plugin-sdk/config-contracts";
import { handleDiscordMessageManagementAction } from "./runtime.messaging.messages.js";
import { handleDiscordReactionMessagingAction } from "./runtime.messaging.reactions.js";
import { handleDiscordMessageSendAction } from "./runtime.messaging.send.js";
import {
  createDiscordMessagingActionContext,
  type DiscordMessagingActionOptions,
} from "./runtime.messaging.shared.js";
export async function handleDiscordMessagingAction(
  action: string,
  params: Record<string, unknown>,
  isActionEnabled: ActionGate<DiscordActionConfig>,
  cfg: AetherConfig,
  options?: DiscordMessagingActionOptions,
): Promise<AgentToolResult<unknown>> {
  if (!cfg) {
    throw new Error("Discord messaging actions require a resolved runtime config.");
  }
  const ctx = createDiscordMessagingActionContext({
    action,
    input: params,
    isActionEnabled,
    cfg,
    options,
  });
  return (
    (await handleDiscordReactionMessagingAction(ctx)) ??
    (await handleDiscordMessageSendAction(ctx)) ??
    (await handleDiscordMessageManagementAction(ctx)) ??
    (() => {
      throw new Error(`Unknown action: ${action}`);
    })()
  );
}
