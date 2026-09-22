// Msteams plugin module implements approval auth behavior.
import { createChannelApprovalAuth } from "aether/plugin-sdk/approval-auth-runtime";
import { normalizeOptionalLowercaseString } from "aether/plugin-sdk/string-coerce-runtime";
import type { AetherConfig } from "../runtime-api.js";
import { normalizeMSTeamsMessagingTarget } from "./resolve-allowlist.js";

const MSTEAMS_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function normalizeMSTeamsApproverId(value: string | number): string | undefined {
  const normalized = normalizeMSTeamsMessagingTarget(String(value));
  const id = normalizeOptionalLowercaseString(
    normalized?.startsWith("user:") ? normalized.slice("user:".length) : normalized,
  );
  return id && MSTEAMS_ID_RE.test(id) ? id : undefined;
}

function resolveMSTeamsChannelConfig(cfg: AetherConfig) {
  return cfg.channels?.msteams;
}

const msTeamsApproval = createChannelApprovalAuth({
  channelLabel: "Microsoft Teams",
  resolveInputs: ({ cfg }) => {
    const channel = resolveMSTeamsChannelConfig(cfg);
    return { allowFrom: channel?.allowFrom, defaultTo: channel?.defaultTo };
  },
  normalizeApprover: normalizeMSTeamsApproverId,
  normalizeSenderId: (value) => {
    const trimmed = normalizeOptionalLowercaseString(value);
    if (!trimmed) {
      return undefined;
    }
    return MSTEAMS_ID_RE.test(trimmed) ? trimmed : undefined;
  },
});

export const getMSTeamsApprovalApprovers = msTeamsApproval.resolveApprovers;
export const msTeamsApprovalAuth = msTeamsApproval.approvalAuth;
