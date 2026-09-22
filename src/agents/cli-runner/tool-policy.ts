import { normalizeToolPolicyName } from "../tool-policy.js";

/** Transport prefix CLI harnesses use for loopback Aether MCP tool names. */
const AETHER_MCP_TOOL_PREFIX = "mcp__aether__";

/** Strips the loopback MCP transport prefix so observers see gateway tool names. */
export function stripAetherMcpToolPrefix(toolName: string): string {
  return toolName.startsWith(AETHER_MCP_TOOL_PREFIX)
    ? toolName.slice(AETHER_MCP_TOOL_PREFIX.length)
    : toolName;
}

/** Keeps only explicit runtime caps for backend-owned exact translation. */
export function resolveCliRuntimeToolsAllow(
  toolsAllow?: string[],
  _toolsAllowIsDefault?: boolean,
): string[] | undefined {
  if (toolsAllow === undefined) {
    return undefined;
  }
  return toolsAllow.some((toolName) => normalizeToolPolicyName(toolName) === "*")
    ? undefined
    : toolsAllow;
}
