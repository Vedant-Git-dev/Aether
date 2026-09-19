/**
 * Aether-owned tool registration filters.
 *
 * Keeps optional tool gating separate from tool construction so config and execution contracts decide exposure.
 */
import { uniqueStrings } from "@aether/normalization-core/string-normalization";
import type { AetherConfig } from "../config/types.aether.js";
import { resolveEffectiveToolPolicy } from "./agent-tools.policy.js";
import { isPrimaryBootstrapRun } from "./bootstrap-routing.js";
import { resolveRequesterToolPolicies } from "./requester-tool-policy.js";
import {
  isRuntimeToolAllowed,
  isToolAllowedByPolicies,
  isToolAllowedByPolicyName,
} from "./tool-policy-match.js";
import {
  expandShippedCoreToolPolicyNames,
  mergeAlsoAllowPolicy,
  resolveToolProfilePolicy,
  type ToolPolicyLike,
} from "./tool-policy.js";
import type { AnyAgentTool } from "./tools/common.js";

function expandProgressCardPolicyNames(
  policy: ToolPolicyLike | undefined,
): ToolPolicyLike | undefined {
  return policy
    ? {
        allow: expandShippedCoreToolPolicyNames(policy.allow),
        deny: expandShippedCoreToolPolicyNames(policy.deny),
      }
    : undefined;
}

/** Drops disabled optional tools while preserving candidate order. */
export function collectPresentAetherTools(
  candidates: readonly (AnyAgentTool | null | undefined)[],
): AnyAgentTool[] {
  return candidates.filter((tool): tool is AnyAgentTool => tool !== null && tool !== undefined);
}

/** Decides whether progress_card should be included in the assembled Aether tool set. */
export function shouldIncludeProgressCardToolForAetherTools(params: {
  agentId?: string;
  agentSessionKey?: string;
  config?: AetherConfig;
  modelId?: string;
  modelProvider?: string;
  pluginToolDenylist?: string[];
  runtimeToolAllowlist?: string[];
}): boolean {
  // `tools.updatePlan` is the shipped kill switch for the replacement progress_card tool.
  if (params.config?.tools?.updatePlan === false) {
    return false;
  }
  if (
    !isToolAllowedByPolicyName("progress_card", {
      deny: expandShippedCoreToolPolicyNames(params.pluginToolDenylist),
    }) ||
    !isRuntimeToolAllowed("progress_card", params.runtimeToolAllowlist)
  ) {
    return false;
  }
  const effective = resolveEffectiveToolPolicy({
    config: params.config,
    sessionKey: params.agentSessionKey,
    agentId: params.agentId,
    modelProvider: params.modelProvider,
    modelId: params.modelId,
  });
  const profilePolicy = mergeAlsoAllowPolicy(
    resolveToolProfilePolicy(effective.profile),
    effective.profileAlsoAllow,
  );
  const providerProfilePolicy = mergeAlsoAllowPolicy(
    resolveToolProfilePolicy(effective.providerProfile),
    effective.providerProfileAlsoAllow,
  );
  return isToolAllowedByPolicies(
    "progress_card",
    [
      profilePolicy,
      providerProfilePolicy,
      effective.globalPolicy,
      effective.globalProviderPolicy,
      effective.agentPolicy,
      effective.agentProviderPolicy,
      resolveRequesterToolPolicies({
        config: params.config,
        agentId: params.agentId,
        sessionKey: params.agentSessionKey,
        senderPolicyMode: "never",
      }).subagentPolicy,
    ].map(expandProgressCardPolicyNames),
  );
}

type PrimarySessionToolRegistrationParams = {
  config?: AetherConfig;
  agentSessionKey?: string;
  pluginToolDenylist?: string[];
};

function shouldIncludePrimarySessionToolForAetherTools(
  toolName: "ask_user" | "secrets",
  params: PrimarySessionToolRegistrationParams,
): boolean {
  const sessionKey = params.agentSessionKey?.trim();
  if (!sessionKey) {
    return false;
  }
  const deny = uniqueStrings([
    ...(params.config?.tools?.deny ?? []),
    ...(params.pluginToolDenylist ?? []),
  ]);
  return isPrimaryBootstrapRun(sessionKey) && isToolAllowedByPolicyName(toolName, { deny });
}

/** Includes ask_user only on a primary session and when normal deny policy permits it. */
export function shouldIncludeAskUserToolForAetherTools(
  params: PrimarySessionToolRegistrationParams,
): boolean {
  return shouldIncludePrimarySessionToolForAetherTools("ask_user", params);
}

/** Keeps credential management on primary sessions allowed by the normal tool policy. */
export function shouldIncludeSecretsToolForAetherTools(
  params: PrimarySessionToolRegistrationParams,
): boolean {
  return shouldIncludePrimarySessionToolForAetherTools("secrets", params);
}
