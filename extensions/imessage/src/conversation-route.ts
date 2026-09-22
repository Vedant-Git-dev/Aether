// Imessage plugin module implements conversation route behavior.
import type { AetherConfig } from "aether/plugin-sdk/config-contracts";
import {
  resolveConfiguredBindingRoute,
  resolveRuntimeConversationBindingRoute,
  type ConfiguredBindingRouteResult,
} from "aether/plugin-sdk/conversation-runtime";
import { resolveAgentRoute } from "aether/plugin-sdk/routing";
import { logVerbose } from "aether/plugin-sdk/runtime-env";
import { resolveIMessageInboundConversationId } from "./conversation-id.js";

export function resolveIMessageConversationRoute(params: {
  cfg: AetherConfig;
  accountId: string;
  isGroup: boolean;
  peerId: string;
  sender: string;
  chatId?: number;
}): ConfiguredBindingRouteResult {
  const route = resolveAgentRoute({
    cfg: params.cfg,
    channel: "imessage",
    accountId: params.accountId,
    peer: {
      kind: params.isGroup ? "group" : "direct",
      id: params.peerId,
    },
  });

  const conversationId = resolveIMessageInboundConversationId({
    isGroup: params.isGroup,
    sender: params.sender,
    chatId: params.chatId,
  });
  if (!conversationId) {
    return { route, bindingResolution: null };
  }

  const conversation = {
    channel: "imessage",
    accountId: params.accountId,
    conversationId,
  };
  const configuredRoute = resolveConfiguredBindingRoute({
    cfg: params.cfg,
    route,
    conversation,
  });

  const runtimeRoute = resolveRuntimeConversationBindingRoute({
    route: configuredRoute.route,
    conversation,
  });
  if (runtimeRoute.bindingRecord && !runtimeRoute.boundSessionKey) {
    logVerbose(`imessage: plugin-bound conversation ${conversationId}`);
  } else if (runtimeRoute.boundSessionKey) {
    logVerbose(
      `imessage: routed via bound conversation ${conversationId} -> ${runtimeRoute.boundSessionKey}`,
    );
  }
  return {
    route: runtimeRoute.route,
    bindingResolution: runtimeRoute.bindingRecord ? null : configuredRoute.bindingResolution,
  };
}
