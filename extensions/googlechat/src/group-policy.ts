import {
  buildChannelGroupsScopeTree,
  resolveScopeRequireMention,
  type ScopeTree,
} from "aether/plugin-sdk/channel-policy";
import type { AetherConfig } from "aether/plugin-sdk/core";

type GroupContext = { cfg: AetherConfig; accountId?: string | null; groupId?: string | null };

export function buildGoogleChatGroupPolicyScope(params: {
  tree: ScopeTree;
  groupId?: string | null;
}) {
  const matchKey =
    params.groupId && Object.hasOwn(params.tree.scopes, params.groupId)
      ? params.groupId
      : undefined;
  return { tree: params.tree, path: matchKey ? [matchKey] : [], matchKey };
}

export function resolveGoogleChatGroupRequireMention(params: GroupContext): boolean {
  return resolveScopeRequireMention(
    buildGoogleChatGroupPolicyScope({
      tree: buildChannelGroupsScopeTree(params.cfg, "googlechat", params.accountId),
      groupId: params.groupId,
    }),
  );
}
