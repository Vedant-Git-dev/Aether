import { normalizeOptionalString } from "@aether/normalization-core/string-coerce";
import {
  ErrorCodes,
  errorShape,
  missingScopeErrorShape,
  validateNodeInvokeParams,
} from "../../../packages/gateway-protocol/src/index.js";
import { captureNodePairingGeneration } from "../../infra/device-pairing-node-state.js";
import {
  isAdminOnlyNodeInvokeCommand,
  isBrowserProxyNodeInvokeCommand,
  isPrivateNodeInvokeCommand,
} from "../../infra/node-commands.js";
import { awaitWithinDeadline, ABSOLUTE_DEADLINE_EXPIRED } from "../../utils/absolute-deadline.js";
import { isForbiddenBrowserProxyMutation } from "../node-browser-proxy-policy.js";
import { isNodeCommandAllowed, resolveNodeCommandAllowlist } from "../node-command-policy.js";
import { applyPluginNodeInvokePolicy } from "../node-invoke-plugin-policy.js";
import { invokeNodeWithReadinessRetry } from "../node-invoke-readiness.js";
import { sanitizeNodeInvokeParamsForForwarding } from "../node-invoke-sanitize.js";
import { captureNodeWakeLifecycle, releaseNodeWakeLifecycle } from "../node-wake-state.js";
import { ADMIN_SCOPE } from "../operator-scopes.js";
import { buildNodeCommandRejectionHint } from "./node-command-rejection-hint.js";
import { nodeInvokePolicy } from "./nodes-policy.js";
import { handleNodeInvokeProgress } from "./nodes.handlers.invoke-progress.js";
import { handleNodeInvokeResult } from "./nodes.handlers.invoke-result.js";
import {
  respondUnavailableOnNodeInvokeErrorWithProvenance,
  parseGatewayPayload,
} from "./nodes.helpers.js";
import {
  isForwardedNodeInvokeApprovalAuthorityActive,
  resolveNodeInvokeRuntimeAuthorityError,
} from "./nodes.invoke-authority.js";
import { emitTalkPttNodeEvent } from "./nodes.invoke-talk-events.js";
import {
  isNodePairingWorkCurrent,
  resolveDispatchableNodeSession,
  respondPairingChanged,
} from "./nodes.shared.js";
import { respondUnavailableOnThrow } from "./response.js";
import type { GatewayRequestHandlers } from "./types.js";
import { assertValidParams } from "./validation.js";

export const nodeInvokeHandlers: GatewayRequestHandlers = {
  "node.invoke": async ({ params, respond, context, client, req, signal }) => {
    if (!assertValidParams(params, validateNodeInvokeParams, "node.invoke", respond)) {
      return;
    }
    const p = params;
    const nodeId = normalizeOptionalString(p.nodeId) ?? "";
    const command = normalizeOptionalString(p.command) ?? "";
    const sessionKey = normalizeOptionalString(p.sessionKey);
    const nodeInvokeStream =
      client?.internal?.syntheticClient === true && client.internal.pluginRuntimeOwnerId
        ? client.internal.nodeInvokeStream
        : undefined;
    if (!nodeId || !command) {
      respond(
        false,
        undefined,
        errorShape(ErrorCodes.INVALID_REQUEST, "nodeId and command required"),
      );
      return;
    }
    if (isPrivateNodeInvokeCommand(command)) {
      respond(
        false,
        undefined,
        errorShape(ErrorCodes.INVALID_REQUEST, "node.invoke does not allow private node controls", {
          details: { command },
        }),
      );
      return;
    }
    if (command === "system.execApprovals.get" || command === "system.execApprovals.set") {
      respond(
        false,
        undefined,
        errorShape(
          ErrorCodes.INVALID_REQUEST,
          "node.invoke does not allow system.execApprovals.*; use exec.approvals.node.*",
          { details: { command } },
        ),
      );
      return;
    }
    if (nodeInvokePolicy.rejectClaudeAgentRun(command, respond)) {
      return;
    }
    if (isBrowserProxyNodeInvokeCommand(command) && isForbiddenBrowserProxyMutation(p.params)) {
      respond(
        false,
        undefined,
        errorShape(
          ErrorCodes.INVALID_REQUEST,
          `node.invoke cannot mutate persistent browser profiles via ${command}`,
          { details: { command } },
        ),
      );
      return;
    }
    if (
      isAdminOnlyNodeInvokeCommand(command) &&
      !nodeInvokePolicy.clientHasOperatorAdminScope(client)
    ) {
      respond(
        false,
        undefined,
        missingScopeErrorShape({ missingScope: ADMIN_SCOPE, requiredScopes: [ADMIN_SCOPE] }),
      );
      return;
    }
    const invokeDeadlineAtMs =
      typeof p.timeoutMs === "number" && p.timeoutMs > 0
        ? performance.now() + p.timeoutMs
        : undefined;
    let nodeCommandDispatched = false;
    const resolveRemainingInvokeTimeoutMs = () =>
      invokeDeadlineAtMs === undefined
        ? p.timeoutMs
        : Math.max(0, invokeDeadlineAtMs - performance.now());
    const respondIfInvokeExpired = () => {
      if (invokeDeadlineAtMs === undefined || resolveRemainingInvokeTimeoutMs() !== 0) {
        return false;
      }
      respondUnavailableOnNodeInvokeErrorWithProvenance(
        respond,
        {
          ok: false,
          error: { code: "TIMEOUT", message: "node invoke timed out" },
        },
        { nodeCommandDispatched },
      );
      return true;
    };
    await respondUnavailableOnThrow(respond, async () => {
      const generation = await awaitWithinDeadline(
        () => captureNodePairingGeneration(nodeId),
        invokeDeadlineAtMs,
        () => performance.now(),
      );
      if (generation === ABSOLUTE_DEADLINE_EXPIRED) {
        respondIfInvokeExpired();
        return;
      }
      if (!generation) {
        respondPairingChanged(respond);
        return;
      }
      const wakeLifecycle = captureNodeWakeLifecycle(nodeId, generation.key);
      // Wake helpers identify their owner by the original signal. Compose the
      // caller only for dispatched node work; never replace that owner signal.
      const invocationLifecycle = signal ? AbortSignal.any([wakeLifecycle, signal]) : wakeLifecycle;
      let releaseApprovalHandoff: (() => void) | undefined;
      try {
        const continuePairingWork = async (): Promise<boolean> => {
          const pairingCurrent = await awaitWithinDeadline(
            () => isNodePairingWorkCurrent({ nodeId, generation, lifecycle: wakeLifecycle }),
            invokeDeadlineAtMs,
            () => performance.now(),
          );
          if (pairingCurrent === ABSOLUTE_DEADLINE_EXPIRED) {
            respondIfInvokeExpired();
            return false;
          }
          if (pairingCurrent) {
            return true;
          }
          respondPairingChanged(respond);
          return false;
        };

        if (respondIfInvokeExpired()) {
          return;
        }

        const cfg = context.getRuntimeConfig();
        let nodeSession = resolveDispatchableNodeSession(
          context.nodeRegistry.getForPairingGeneration(nodeId, generation.key),
        );
        if (!nodeSession) {
          // Mobile push wake (APNs) has been removed; a node that is not
          // connected cannot be woken remotely.
          respond(
            false,
            undefined,
            errorShape(ErrorCodes.UNAVAILABLE, "node not connected", {
              details: {
                code: "NOT_CONNECTED",
                nodeError: { code: "NOT_CONNECTED", message: "node not connected" },
                nodeCommandDispatched: false,
              },
            }),
          );
          return;
        }
        // A reload may revoke authority for an in-flight request, but it must not
        // retroactively grant one that was denied when admitted before node wake.
        for (const authorizationCfg of [cfg, context.getRuntimeConfig()]) {
          const allowlist = resolveNodeCommandAllowlist(authorizationCfg, {
            ...nodeSession,
            approvedCommands: nodeSession.commands,
          });
          const allowed = isNodeCommandAllowed({
            command,
            declaredCommands: nodeSession.commands,
            allowlist,
          });
          if (!allowed.ok) {
            const hint = buildNodeCommandRejectionHint(
              allowed.reason,
              command,
              nodeSession,
              authorizationCfg,
            );
            respond(
              false,
              undefined,
              errorShape(ErrorCodes.INVALID_REQUEST, hint, {
                details: { reason: allowed.reason, command },
              }),
            );
            return;
          }
        }

        const forwardedParams = sanitizeNodeInvokeParamsForForwarding({
          nodeId,
          command,
          rawParams: p.params,
          client,
          execApprovalManager: context.execApprovalManager,
        });
        if (!forwardedParams.ok) {
          respond(
            false,
            undefined,
            errorShape(ErrorCodes.INVALID_REQUEST, forwardedParams.message, {
              details: forwardedParams.details ?? null,
            }),
          );
          return;
        }
        if (respondIfInvokeExpired()) {
          return;
        }
        if (forwardedParams.approvalAuthority) {
          const authority = forwardedParams.approvalAuthority;
          releaseApprovalHandoff =
            context.execApprovalManager?.retainForHandoff(authority.recordId) ?? undefined;
          if (!releaseApprovalHandoff) {
            respond(
              false,
              undefined,
              errorShape(
                ErrorCodes.INVALID_REQUEST,
                "approved runtime authority closed before node dispatch",
                { details: { code: "APPROVAL_AUTHORITY_CLOSED" } },
              ),
            );
            return;
          }
        }
        const isForwardedApprovalAuthorityActive = () =>
          isForwardedNodeInvokeApprovalAuthorityActive({
            manager: context.execApprovalManager,
            authority: forwardedParams.approvalAuthority,
          });
        const policyResult = await awaitWithinDeadline(
          () =>
            applyPluginNodeInvokePolicy({
              context,
              client,
              nodeSession,
              command,
              params: forwardedParams.params,
              ...(sessionKey ? { sessionKey } : {}),
              turnSource: {
                channel: p.turnSourceChannel,
                to: p.turnSourceTo,
                accountId: p.turnSourceAccountId,
                threadId: p.turnSourceThreadId,
              },
              timeoutMs: p.timeoutMs,
              deadlineAtMs: invokeDeadlineAtMs,
              signal: invocationLifecycle,
              resolveRemainingTimeoutMs: resolveRemainingInvokeTimeoutMs,
              onNodeCommandDispatched: () => {
                // Deadline races must retain transport ownership so a command
                // already handed to the node is never advertised as retry-safe.
                nodeCommandDispatched = true;
              },
              idempotencyKey: p.idempotencyKey,
              isInvocationCurrent: () =>
                isNodePairingWorkCurrent({ nodeId, generation, lifecycle: wakeLifecycle }),
              isApprovalAuthorityActive: isForwardedApprovalAuthorityActive,
              ...(nodeInvokeStream ? { nodeInvokeStream } : {}),
            }),
          invokeDeadlineAtMs,
          () => performance.now(),
        );
        if (policyResult === ABSOLUTE_DEADLINE_EXPIRED) {
          respondIfInvokeExpired();
          return;
        }
        if (!(await continuePairingWork())) {
          return;
        }
        if (policyResult) {
          // Plugin policies can satisfy an invocation without crossing the raw
          // node command channel; still emit mirrored Talk events for UI state.
          if (!policyResult.ok) {
            const errorCode = policyResult.unavailable
              ? ErrorCodes.UNAVAILABLE
              : ErrorCodes.INVALID_REQUEST;
            respond(
              false,
              undefined,
              errorShape(errorCode, policyResult.message, {
                details: {
                  ...policyResult.details,
                  ...(policyResult.code ? { code: policyResult.code } : {}),
                },
              }),
            );
            return;
          }
          const payload = policyResult.payloadJSON
            ? parseGatewayPayload(policyResult.payloadJSON)
            : policyResult.payload;
          emitTalkPttNodeEvent({
            context,
            nodeId,
            command,
            payload,
          });
          respond(
            true,
            {
              ok: true,
              nodeId,
              command,
              payload: policyResult.payload,
              payloadJSON: policyResult.payloadJSON ?? null,
            },
            undefined,
          );
          return;
        }
        const dispatchSession = resolveDispatchableNodeSession(
          context.nodeRegistry.getForPairingGeneration(nodeId, generation.key),
        );
        if (!dispatchSession || dispatchSession.connId !== nodeSession.connId) {
          respond(
            false,
            undefined,
            errorShape(ErrorCodes.UNAVAILABLE, "node connection changed before dispatch", {
              retryable: true,
              details: { code: "ROUTE_CHANGED" },
            }),
          );
          return;
        }
        const resolveDispatchAuthorization = (dispatchCfg: typeof cfg) =>
          isNodeCommandAllowed({
            command,
            declaredCommands: dispatchSession.commands,
            allowlist: resolveNodeCommandAllowlist(dispatchCfg, {
              ...dispatchSession,
              approvedCommands: dispatchSession.commands,
            }),
          });
        const dispatchCfg = context.getRuntimeConfig();
        const dispatchAllowed = resolveDispatchAuthorization(dispatchCfg);
        if (!dispatchAllowed.ok) {
          respond(
            false,
            undefined,
            errorShape(
              ErrorCodes.INVALID_REQUEST,
              buildNodeCommandRejectionHint(
                dispatchAllowed.reason,
                command,
                dispatchSession,
                dispatchCfg,
              ),
              { details: { reason: dispatchAllowed.reason, command } },
            ),
          );
          return;
        }
        const dispatchTimeoutMs = resolveRemainingInvokeTimeoutMs();
        if (invokeDeadlineAtMs !== undefined && dispatchTimeoutMs === 0) {
          respondIfInvokeExpired();
          return;
        }
        // Policy, pairing, and approval checks above may await. Revalidate the
        // exact runtime capability at the final raw transport handoff.
        const authorityError = resolveNodeInvokeRuntimeAuthorityError({
          context,
          client,
          approvalAuthority: forwardedParams.approvalAuthority,
        });
        if (authorityError) {
          respond(
            false,
            undefined,
            errorShape(ErrorCodes.INVALID_REQUEST, authorityError, {
              details: { code: "APPROVAL_AUTHORITY_CLOSED" },
            }),
          );
          return;
        }
        const res = await invokeNodeWithReadinessRetry(context.nodeRegistry, {
          nodeId,
          expectedConnId: nodeSession.connId,
          expectedPairingGeneration: generation.key,
          command,
          params: forwardedParams.params,
          timeoutMs: dispatchTimeoutMs,
          deadlineAtMs: invokeDeadlineAtMs,
          signal: invocationLifecycle,
          idempotencyKey: p.idempotencyKey,
          ...(sessionKey ? { sessionKey } : {}),
          ...(nodeInvokeStream && {
            onProgress: nodeInvokeStream.onProgress,
            idleTimeoutMs: nodeInvokeStream.idleTimeoutMs,
          }),
          isDispatchAuthorized: () =>
            (nodeInvokeStream?.isRuntimeCurrent() ?? true) &&
            resolveNodeInvokeRuntimeAuthorityError({
              context,
              client,
              approvalAuthority: forwardedParams.approvalAuthority,
            }) === undefined,
          onDispatchReady: (invokeId) => {
            nodeCommandDispatched = true;
            nodeInvokeStream?.onDispatchReady(invokeId);
          },
        });
        if (!(await continuePairingWork())) {
          return;
        }
        if (!res.ok) {
          if (
            !respondUnavailableOnNodeInvokeErrorWithProvenance(respond, res, {
              nodeCommandDispatched,
            })
          ) {
            return;
          }
          return;
        }
        const payload = res.payloadJSON ? parseGatewayPayload(res.payloadJSON) : res.payload;
        emitTalkPttNodeEvent({
          context,
          nodeId,
          command,
          payload,
        });
        respond(
          true,
          {
            ok: true,
            nodeId,
            command,
            payload,
            payloadJSON: res.payloadJSON ?? null,
          },
          undefined,
        );
      } finally {
        releaseApprovalHandoff?.();
        releaseNodeWakeLifecycle(nodeId, wakeLifecycle);
      }
    });
  },
  "node.invoke.progress": handleNodeInvokeProgress,
  "node.invoke.result": handleNodeInvokeResult,
};
