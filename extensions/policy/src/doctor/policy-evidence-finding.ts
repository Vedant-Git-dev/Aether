import type { HealthFinding } from "aether/plugin-sdk/health";
import { POLICY_CHECK_IDS } from "./check-ids.js";

export function policyEvidenceFinding(
  entry: { readonly source: string },
  params: {
    readonly checkId: (typeof POLICY_CHECK_IDS)[number];
    readonly message: string;
    readonly requirement: string;
    readonly fixHint: string;
  },
): HealthFinding {
  return {
    checkId: params.checkId,
    severity: "error",
    message: params.message,
    source: "policy",
    path: "aether config",
    ocPath: entry.source,
    target: entry.source,
    requirement: params.requirement,
    fixHint: params.fixHint,
  };
}
