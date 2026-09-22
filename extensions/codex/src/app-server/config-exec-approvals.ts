import {
  execPolicy,
  type EmbeddedRunAttemptParamsV2,
} from "aether/plugin-sdk/agent-harness-runtime";
import { resolveAgentConfig } from "aether/plugin-sdk/agent-scope-runtime";
import type { AetherConfig } from "aether/plugin-sdk/config-contracts";
import {
  resolveExecApprovalsFromFile,
  type ExecApprovalsFile,
} from "aether/plugin-sdk/exec-approvals-runtime";
import type {
  AetherExecApprovalFloorsForCodexAppServer,
  AetherExecMode,
  AetherExecPolicy,
  AetherExecPolicyForCodexAppServer,
} from "./config-contracts.js";
import { readExecAsk, readExecSecurity, readRecord } from "./config-utils.js";

function resolveAetherExecPolicyFromConfig(params: {
  config?: AetherConfig;
  agentId?: string;
}): AetherExecPolicy {
  const globalExec = readRecord(params.config?.tools?.exec);
  const globalPolicy = applyAetherExecPolicyLayer(createDefaultAetherExecPolicy(), globalExec);
  const agentId = params.agentId?.trim();
  const agentExec = agentId
    ? readRecord(resolveAgentConfig(params.config ?? {}, agentId)?.tools?.exec)
    : undefined;
  return applyAetherExecPolicyLayer(globalPolicy, agentExec);
}

export function resolveAetherExecPolicyForCodexAppServer(params: {
  permissionMode?: EmbeddedRunAttemptParamsV2["permissionMode"];
  execOverrides?: {
    mode?: unknown;
    security?: unknown;
    ask?: unknown;
  };
  approvals?: ExecApprovalsFile;
  config?: AetherConfig;
  agentId?: string;
}): AetherExecPolicyForCodexAppServer {
  if (params.permissionMode === "full") {
    return { ...resolveAetherExecPolicyForMode("full"), touched: true };
  }
  const basePolicy = resolveAetherExecPolicyFromConfig({
    config: params.config,
    agentId: params.agentId,
  });
  const overridePolicy = applyAetherExecPolicyLayer(basePolicy, params.execOverrides);
  const approvalFloors = resolveAetherExecApprovalFloorsForCodexAppServer({
    approvals: params.approvals,
    agentId: params.agentId,
    policy: overridePolicy,
  });
  return applyAetherExecApprovalFloors(overridePolicy, approvalFloors);
}

function createDefaultAetherExecPolicy(): AetherExecPolicy {
  return {
    ...resolveAetherExecPolicyForMode("full"),
    touched: false,
  };
}

function applyAetherExecPolicyLayer(
  base: AetherExecPolicy,
  exec?: { mode?: unknown; security?: unknown; ask?: unknown },
): AetherExecPolicy {
  if (!exec) {
    return base;
  }
  const mode = readExecMode(exec.mode);
  if (mode !== undefined) {
    return {
      ...resolveAetherExecPolicyForMode(mode),
      touched: true,
    };
  }
  const security = readExecSecurity(exec.security);
  const ask = readExecAsk(exec.ask);
  if (security === undefined && ask === undefined) {
    return base;
  }
  const nextSecurity = security ?? base.security;
  const nextAsk = ask ?? base.ask;
  return {
    mode: execPolicy.resolveExecModePolicy({ security: nextSecurity, ask: nextAsk }).mode,
    security: nextSecurity,
    ask: nextAsk,
    touched: true,
  };
}

function resolveAetherExecApprovalFloorsForCodexAppServer(params: {
  approvals?: ExecApprovalsFile;
  agentId?: string;
  policy: AetherExecPolicy;
}): AetherExecApprovalFloorsForCodexAppServer | undefined {
  if (!params.approvals) {
    return undefined;
  }
  return resolveExecApprovalsFromFile({
    file: params.approvals,
    agentId: params.agentId,
    overrides: {
      security: params.policy.security,
      ask: params.policy.ask,
    },
  }).agent;
}

function applyAetherExecApprovalFloors(
  base: AetherExecPolicy,
  approvalFloors?: AetherExecApprovalFloorsForCodexAppServer,
): AetherExecPolicy {
  if (!approvalFloors) {
    return base;
  }
  const nextSecurity = approvalFloors.security
    ? execPolicy.minSecurity(base.security, approvalFloors.security)
    : base.security;
  const nextAsk = approvalFloors.ask ? execPolicy.maxAsk(base.ask, approvalFloors.ask) : base.ask;
  if (nextSecurity === base.security && nextAsk === base.ask) {
    return base;
  }
  return {
    mode: execPolicy.resolveExecModePolicy({ security: nextSecurity, ask: nextAsk }).mode,
    security: nextSecurity,
    ask: nextAsk,
    touched: true,
  };
}

function resolveAetherExecPolicyForMode(
  mode: AetherExecMode,
): Omit<AetherExecPolicy, "touched"> {
  const { security, ask } = execPolicy.resolveExecModePolicy({
    mode,
    security: "full",
    ask: "off",
  });
  return { mode, security, ask };
}

function readExecMode(value: unknown): AetherExecMode | undefined {
  return value === "deny" ||
    value === "allowlist" ||
    value === "ask" ||
    value === "auto" ||
    value === "full"
    ? value
    : undefined;
}
