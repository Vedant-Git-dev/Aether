/**
 * Shared contract between the aether-tools MCP stdio entry and the callers
 * that inject it into CLI harness runs. Keep this module free of MCP SDK and
 * tool-runtime imports so CLI-runner prepare paths can build server configs
 * without loading the server.
 */
import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import type { SystemAgentToolOptions } from "../agents/tools/system-agent-tool.js";
import { resolveAetherPackageRootSync } from "../infra/aether-root.js";
import type { BundleMcpConfig } from "../plugins/bundle-mcp.js";

export const AETHER_TOOLS_MCP_TOOLS_ENV = "AETHER_TOOLS_MCP_TOOLS";
export const AETHER_TOOLS_MCP_SYSTEM_AGENT_SURFACE_ENV =
  "AETHER_TOOLS_MCP_SYSTEM_AGENT_SURFACE";
export const AETHER_TOOLS_MCP_SYSTEM_AGENT_APPROVAL_ARMED_ENV =
  "AETHER_TOOLS_MCP_SYSTEM_AGENT_APPROVAL_ARMED";
export const AETHER_TOOLS_MCP_SYSTEM_AGENT_PROPOSAL_ENV =
  "AETHER_TOOLS_MCP_SYSTEM_AGENT_PROPOSAL";
// Delegation and chat consent are mutually exclusive. Keep both in the existing
// per-turn transport value so native transcript resume identity stays stable.
const APPROVAL_ARMED_OPERATOR_ONLY_VALUE = "operator-only";

const AETHER_TOOLS_MCP_TOOL_IDS = ["cron", "aether"] as const;
export type AetherToolsMcpToolId = (typeof AETHER_TOOLS_MCP_TOOL_IDS)[number];

function isAetherToolsMcpToolId(value: string): value is AetherToolsMcpToolId {
  return (AETHER_TOOLS_MCP_TOOL_IDS as readonly string[]).includes(value);
}

/** Parse the served tool selection; the default stays cron for acpx bridges. */
export function resolveAetherToolsMcpToolSelection(
  env: NodeJS.ProcessEnv = process.env,
): AetherToolsMcpToolId[] {
  const raw = env[AETHER_TOOLS_MCP_TOOLS_ENV]?.trim();
  if (!raw) {
    return ["cron"];
  }
  const entries = raw
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
  const selection = entries.filter(isAetherToolsMcpToolId);
  if (selection.length === 0 || selection.length !== entries.length) {
    throw new Error(
      `${AETHER_TOOLS_MCP_TOOLS_ENV} must be a comma list of: ${AETHER_TOOLS_MCP_TOOL_IDS.join(", ")}`,
    );
  }
  return selection;
}

/** Parse the Aether surface for served aether tools; defaults to cli. */
export function resolveAetherToolsMcpSystemAgentSurface(
  env: NodeJS.ProcessEnv = process.env,
): SystemAgentToolOptions["surface"] {
  const raw = env[AETHER_TOOLS_MCP_SYSTEM_AGENT_SURFACE_ENV]?.trim();
  if (!raw || raw === "cli") {
    return "cli";
  }
  if (raw === "gateway") {
    return "gateway";
  }
  throw new Error(`${AETHER_TOOLS_MCP_SYSTEM_AGENT_SURFACE_ENV} must be "cli" or "gateway"`);
}

/**
 * Reconstruct per-turn approval state for the served aether tool. The
 * stdio server runs out of process, so the host passes the armed bit and the
 * pending proposal hash through env; the host mirrors transitions back from
 * tool events (see mirrorSystemAgentToolStateFromEvents in agent-turn.ts).
 */
export function resolveAetherToolsMcpSystemAgentApproval(env: NodeJS.ProcessEnv = process.env): {
  approvalArmed: boolean;
  proposalRef: { current?: string };
  operatorApprovalOnly?: boolean;
} {
  const pendingProposal = env[AETHER_TOOLS_MCP_SYSTEM_AGENT_PROPOSAL_ENV]?.trim();
  const armedValue = env[AETHER_TOOLS_MCP_SYSTEM_AGENT_APPROVAL_ARMED_ENV]?.trim();
  return {
    approvalArmed: armedValue === "1",
    proposalRef: pendingProposal ? { current: pendingProposal } : {},
    ...(armedValue === APPROVAL_ARMED_OPERATOR_ONLY_VALUE ? { operatorApprovalOnly: true } : {}),
  };
}

function resolveTsxImportSpecifier(): string {
  try {
    return createRequire(import.meta.url).resolve("tsx");
  } catch {
    return "tsx";
  }
}

function resolveAetherToolsServeCommand(): { command: string; args: string[] } {
  const packageRoot = resolveAetherPackageRootSync({
    argv1: process.argv[1],
    moduleUrl: import.meta.url,
    cwd: process.cwd(),
  });
  if (!packageRoot) {
    throw new Error("aether-tools MCP: could not resolve the Aether package root");
  }
  const distEntry = path.join(packageRoot, "dist", "mcp", "aether-tools-serve.js");
  if (fs.existsSync(distEntry)) {
    return { command: process.execPath, args: [distEntry] };
  }
  const sourceEntry = path.join(packageRoot, "src", "mcp", "aether-tools-serve.ts");
  if (!fs.existsSync(sourceEntry)) {
    throw new Error(`aether-tools MCP: no serve entry under ${packageRoot}`);
  }
  // Bun executes TypeScript entries directly; Node source checkouts need tsx.
  if (process.versions.bun) {
    return { command: process.execPath, args: [sourceEntry] };
  }
  return {
    command: process.execPath,
    args: ["--import", resolveTsxImportSpecifier(), sourceEntry],
  };
}

/**
 * Aether CLI-harness runs get exactly one MCP server: this stdio entry
 * serving the ring-zero aether tool. The server keeps the "aether" name
 * so backend tool pre-approvals (e.g. Claude's --allowedTools mcp__aether__*)
 * apply without per-backend argument surgery.
 */
export function buildSystemAgentToolsMcpServerConfig(
  options: SystemAgentToolOptions,
): BundleMcpConfig {
  const entry = resolveAetherToolsServeCommand();
  const pendingProposal = options.proposalRef?.current;
  return {
    mcpServers: {
      aether: {
        command: entry.command,
        args: options.agentId
          ? [...entry.args, "--aether-agent-id", options.agentId]
          : entry.args,
        env: {
          [AETHER_TOOLS_MCP_TOOLS_ENV]: "aether" satisfies AetherToolsMcpToolId,
          [AETHER_TOOLS_MCP_SYSTEM_AGENT_SURFACE_ENV]: options.surface,
          // Per-turn approval state travels with the per-run MCP config; the
          // host mirrors proposal transitions back from tool events.
          ...(options.operatorApprovalOnly === true
            ? {
                [AETHER_TOOLS_MCP_SYSTEM_AGENT_APPROVAL_ARMED_ENV]:
                  APPROVAL_ARMED_OPERATOR_ONLY_VALUE,
              }
            : options.approvalArmed === true
              ? { [AETHER_TOOLS_MCP_SYSTEM_AGENT_APPROVAL_ARMED_ENV]: "1" }
              : {}),
          ...(pendingProposal
            ? { [AETHER_TOOLS_MCP_SYSTEM_AGENT_PROPOSAL_ENV]: pendingProposal }
            : {}),
        },
      },
    },
  };
}
