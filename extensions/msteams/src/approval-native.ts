import { DEFAULT_ACCOUNT_ID, normalizeAccountId } from "aether/plugin-sdk/account-id";
import { createApproverRestrictedNativeApprovalCapability } from "aether/plugin-sdk/approval-delivery-runtime";
import { createLazyChannelApprovalNativeRuntimeAdapter } from "aether/plugin-sdk/approval-handler-adapter-runtime";
import type { ChannelApprovalKind } from "aether/plugin-sdk/approval-handler-runtime";
import {
  createChannelApproverDmTargetResolver,
  createChannelNativeOriginTargetResolver,
  createNativeApprovalChannelRouteGates,
  createNativeApprovalMessagingTargetResolvers,
  shouldSuppressLocalNativeExecApprovalPrompt,
} from "aether/plugin-sdk/approval-native-runtime";
import type {
  ExecApprovalRequest,
  PluginApprovalRequest,
  SystemAgentApprovalRequest,
} from "aether/plugin-sdk/approval-runtime";
import type {
  ChannelApprovalCapability,
  ChannelOutboundPayloadHint,
} from "aether/plugin-sdk/channel-contract";
import type { AetherConfig } from "aether/plugin-sdk/config-contracts";
import type { ReplyPayload } from "aether/plugin-sdk/reply-runtime";
import { normalizeOptionalString } from "aether/plugin-sdk/string-coerce-runtime";
import { getMSTeamsApprovalApprovers, msTeamsApprovalAuth } from "./approval-auth.js";
import { msteamsConfigAdapter, resolveMSTeamsAccount } from "./channel-config.js";
import { normalizeMSTeamsMessagingTarget } from "./resolve-allowlist.js";

type MSTeamsApprovalRequest =
  | ExecApprovalRequest
  | PluginApprovalRequest
  | SystemAgentApprovalRequest;

function isMSTeamsApprovalTransportEnabled(params: {
  cfg: AetherConfig;
  accountId?: string | null;
}): boolean {
  if (params.accountId && normalizeAccountId(params.accountId) !== DEFAULT_ACCOUNT_ID) {
    return false;
  }
  const account = resolveMSTeamsAccount(params.cfg);
  return account.enabled && account.configured && account.tokenStatus === "available";
}

const msTeamsMessagingTargetResolvers = createNativeApprovalMessagingTargetResolvers({
  channel: "msteams",
  normalizeTo: normalizeMSTeamsMessagingTarget,
});

const msTeamsApprovalTargetResolvers = {
  ...msTeamsMessagingTargetResolvers,
  resolveTurnSourceTarget: (request: MSTeamsApprovalRequest) => {
    const target = msTeamsMessagingTargetResolvers.resolveTurnSourceTarget(request);
    return target ? { ...target, threadId: request.request.turnSourceThreadId ?? null } : null;
  },
  resolveSessionTarget: (
    sessionTarget: Parameters<typeof msTeamsMessagingTargetResolvers.resolveSessionTarget>[0],
  ) => {
    const target = msTeamsMessagingTargetResolvers.resolveSessionTarget(sessionTarget);
    return target ? { ...target, threadId: sessionTarget.threadId ?? null } : null;
  },
};

const msTeamsApprovalRouteGates = createNativeApprovalChannelRouteGates({
  channel: "msteams",
  defaultForwardingMode: "session",
  isTransportEnabled: isMSTeamsApprovalTransportEnabled,
  listAccountIds: msteamsConfigAdapter.listAccountIds,
  resolveDefaultAccountId: () => DEFAULT_ACCOUNT_ID,
  normalizeForwardTarget: msTeamsApprovalTargetResolvers.normalizeForwardTarget,
  resolveTurnSourceTarget: msTeamsApprovalTargetResolvers.resolveTurnSourceTarget,
});

export function isMSTeamsNativeApprovalClientEnabled(params: {
  cfg: AetherConfig;
  accountId?: string | null;
}): boolean {
  return (
    msTeamsApprovalRouteGates.canAnyApprovalPotentiallyRouteToChannel({
      ...params,
      nativeSessionOnly: true,
    }) && getMSTeamsApprovalApprovers(params).length > 0
  );
}

export function shouldHandleMSTeamsNativeApprovalRequest(params: {
  cfg: AetherConfig;
  accountId?: string | null;
  approvalKind?: ChannelApprovalKind;
  request: MSTeamsApprovalRequest;
}): boolean {
  return (
    msTeamsApprovalRouteGates.shouldHandleApprovalRequest(params) &&
    getMSTeamsApprovalApprovers(params).length > 0 &&
    Boolean(msTeamsApprovalTargetResolvers.resolveTurnSourceTarget(params.request))
  );
}

export function shouldSuppressLocalMSTeamsExecApprovalPrompt(params: {
  cfg: AetherConfig;
  accountId?: string | null;
  payload: ReplyPayload;
  hint?: ChannelOutboundPayloadHint;
}): boolean {
  return shouldSuppressLocalNativeExecApprovalPrompt({
    ...params,
    isNativeDeliveryEnabled: isMSTeamsNativeApprovalClientEnabled,
  });
}

const resolveMSTeamsOriginTarget = createChannelNativeOriginTargetResolver({
  channel: "msteams",
  shouldHandleRequest: shouldHandleMSTeamsNativeApprovalRequest,
  resolveTurnSourceTarget: msTeamsApprovalTargetResolvers.resolveTurnSourceTarget,
  resolveSessionTarget: msTeamsApprovalTargetResolvers.resolveSessionTarget,
  normalizeTarget: msTeamsApprovalTargetResolvers.normalizeTarget,
});

const msTeamsLazyApprovalNativeRuntime = createLazyChannelApprovalNativeRuntimeAdapter({
  capabilityBoundary: true,
  eventKinds: ["exec", "plugin", "system-agent"],
  isConfigured: ({ cfg, accountId }) => isMSTeamsNativeApprovalClientEnabled({ cfg, accountId }),
  shouldHandle: ({ cfg, accountId, approvalKind, request }) =>
    shouldHandleMSTeamsNativeApprovalRequest({ cfg, accountId, approvalKind, request }),
  load: async () => {
    const { msTeamsApprovalNativeRuntime } = await import("./approval-handler.runtime.js");
    return msTeamsApprovalNativeRuntime;
  },
});

const resolveMSTeamsApproverDmTargets = createChannelApproverDmTargetResolver({
  shouldHandleRequest: shouldHandleMSTeamsNativeApprovalRequest,
  resolveApprovers: getMSTeamsApprovalApprovers,
  mapApprover: (approver, params) => ({
    to: `user:${approver}`,
    accountId: normalizeOptionalString(params.accountId),
  }),
});

const msTeamsNativeApprovalCapability = createApproverRestrictedNativeApprovalCapability({
  channel: "msteams",
  channelLabel: "Microsoft Teams",
  describeExecApprovalSetup: () =>
    "Approve it from the Web UI or terminal UI for now. Microsoft Teams supports native approvals when the bot is configured. Configure `channels.msteams.allowFrom` or `channels.msteams.defaultTo` with Microsoft Entra object ID approvers.",
  listAccountIds: msteamsConfigAdapter.listAccountIds,
  hasApprovers: ({ cfg, accountId }) => getMSTeamsApprovalApprovers({ cfg, accountId }).length > 0,
  isExecAuthorizedSender: ({ cfg, accountId, senderId }) =>
    msTeamsApprovalAuth.authorizeActorAction?.({
      cfg,
      accountId,
      senderId,
      action: "approve",
      approvalKind: "exec",
    })?.authorized ?? false,
  isPluginAuthorizedSender: ({ cfg, accountId, senderId }) =>
    msTeamsApprovalAuth.authorizeActorAction?.({
      cfg,
      accountId,
      senderId,
      action: "approve",
      approvalKind: "plugin",
    })?.authorized ?? false,
  isNativeDeliveryEnabled: isMSTeamsNativeApprovalClientEnabled,
  resolveNativeDeliveryMode: () => "channel",
  requireMatchingTurnSourceChannel: true,
  resolveSuppressionAccountId: ({ target, request }) =>
    normalizeOptionalString(target.accountId) ??
    normalizeOptionalString(request.request.turnSourceAccountId),
  resolveOriginTarget: resolveMSTeamsOriginTarget,
  resolveApproverDmTargets: resolveMSTeamsApproverDmTargets,
  nativeRuntime: msTeamsLazyApprovalNativeRuntime,
});

export const msTeamsApprovalCapability: ChannelApprovalCapability = {
  ...msTeamsNativeApprovalCapability,
  // Preserve implicit same-chat authorization when no explicit approvers exist.
  authorizeActorAction: (params) => msTeamsApprovalAuth.authorizeActorAction?.(params),
};
