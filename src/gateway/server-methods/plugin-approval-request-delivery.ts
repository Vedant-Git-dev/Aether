import type { PluginApprovalRequestPayload } from "../../infra/plugin-approvals.js";
import { runApprovalRequestDeliveries } from "./approval-request-delivery.js";
import { buildRequestedApprovalEvent, handlePendingApprovalRequest } from "./approval-shared.js";
import type { GatewayRequestContext } from "./types.js";

type PendingPluginApproval = Pick<
  Parameters<typeof handlePendingApprovalRequest<PluginApprovalRequestPayload>>[0],
  "manager" | "record" | "respond" | "context" | "clientConnId" | "twoPhase"
>;

export function handlePendingPluginApprovalRequest(
  params: PendingPluginApproval & {
    forwardRequest: GatewayRequestContext["forwardPluginApprovalRequest"];
    source: "rpc" | "node-policy";
  },
): Promise<void> {
  const { forwardRequest, source, ...pending } = params;
  const requestEvent = buildRequestedApprovalEvent(pending.record, "plugin");
  const logContext = source === "node-policy" ? "node policy " : "";
  return handlePendingApprovalRequest({
    ...pending,
    requestEventName: "plugin.approval.requested",
    requestEvent,
    approvalKind: "plugin",
    deliverRequest: () =>
      runApprovalRequestDeliveries({
        context: pending.context,
        record: pending.record,
        forward: forwardRequest
          ? [
              () => forwardRequest(requestEvent),
              `plugin approvals: forward ${logContext}request failed`,
            ]
          : undefined,
      }),
  });
}
