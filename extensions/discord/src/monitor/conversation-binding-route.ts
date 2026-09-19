import type { AetherConfig } from "aether/plugin-sdk/config-contracts";
import {
  resolveConfiguredBindingRoute,
  resolveRuntimeConversationBindingRoute,
} from "aether/plugin-sdk/conversation-binding-runtime";
import type { ResolvedAgentRoute } from "aether/plugin-sdk/routing";
import { logVerbose } from "aether/plugin-sdk/runtime-env";
import { shouldIgnoreStaleDiscordRouteBinding } from "./route-resolution.js";

export function resolveDiscordConversationBindingRoute(params: {
  cfg: AetherConfig;
  route: ResolvedAgentRoute;
  accountId: string;
  runtimeConversationId: string;
  configuredConversationId: string;
  parentConversationId?: string;
  touchBinding?: boolean;
}) {
  let runtimeRoute = resolveRuntimeConversationBindingRoute({
    route: params.route,
    touchBinding: params.touchBinding,
    conversation: {
      channel: "discord",
      accountId: params.accountId,
      conversationId: params.runtimeConversationId,
      parentConversationId: params.parentConversationId,
    },
  });
  if (
    shouldIgnoreStaleDiscordRouteBinding({
      bindingRecord: runtimeRoute.bindingRecord,
      route: params.route,
    })
  ) {
    logVerbose(
      `discord: ignoring stale route binding for conversation ${params.runtimeConversationId} (${runtimeRoute.bindingRecord?.targetSessionKey} -> ${params.route.sessionKey})`,
    );
    runtimeRoute = { bindingOwnerAvailable: true, bindingRecord: null, route: params.route };
  }
  const configuredRoute = runtimeRoute.bindingRecord
    ? null
    : resolveConfiguredBindingRoute({
        cfg: params.cfg,
        route: params.route,
        conversation: {
          channel: "discord",
          accountId: params.accountId,
          conversationId: params.configuredConversationId,
          parentConversationId: params.parentConversationId,
        },
      });
  return { runtimeRoute, configuredRoute };
}
