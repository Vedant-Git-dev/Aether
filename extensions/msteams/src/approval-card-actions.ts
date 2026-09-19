import type { ChannelApprovalKind } from "aether/plugin-sdk/approval-handler-runtime";
import {
  createNativeApprovalControlRegistry,
  type ExecApprovalDecision,
} from "aether/plugin-sdk/approval-runtime";
import {
  isRecord,
  normalizeOptionalLowercaseString,
  normalizeOptionalString,
} from "aether/plugin-sdk/string-coerce-runtime";

export type MSTeamsApprovalCardBinding = {
  token: string;
  accountId: string;
  approvalId: string;
  approvalKind: ChannelApprovalKind;
  decision: ExecApprovalDecision;
  allowedDecisions: readonly ExecApprovalDecision[];
  conversationId: string;
  activityId: string;
  expiresAtMs: number;
};

export const msTeamsApprovalControls =
  createNativeApprovalControlRegistry<MSTeamsApprovalCardBinding>({
    releaseClaimOnLookupExpiry: true,
  });

export function readMSTeamsApprovalActionToken(value: unknown): string | null {
  if (!isRecord(value)) {
    return null;
  }
  const action = isRecord(value.action) ? value.action : undefined;
  const submitted =
    action &&
    normalizeOptionalLowercaseString(action.type) === "action.submit" &&
    isRecord(action.data)
      ? action.data
      : value;
  if (submitted.aetherAction !== "approval") {
    return null;
  }
  return normalizeOptionalString(submitted.token) ?? null;
}
