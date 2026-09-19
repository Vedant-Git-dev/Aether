import type { SessionRow } from "@aether/gateway-protocol";

export type { AgentsListResult } from "@aether/gateway-protocol";
export type GatewaySessionRow = SessionRow & {
  hasActiveRun?: boolean;
  abortedLastRun?: boolean;
};
