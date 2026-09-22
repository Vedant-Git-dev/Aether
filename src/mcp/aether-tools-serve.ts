/**
 * Standalone MCP server for selected built-in Aether tools.
 *
 * Run via: node --import tsx src/mcp/aether-tools-serve.ts
 * Or: bun src/mcp/aether-tools-serve.ts
 */
import { pathToFileURL } from "node:url";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { AUTOMATIONS_TOOL_NAME } from "../agents/tools/automations-tool-name.js";
import type { AnyAgentTool } from "../agents/tools/common.js";
import { createCronTool } from "../agents/tools/cron-tool.js";
import { createSystemAgentTool } from "../agents/tools/system-agent-tool.js";
import type { SystemAgentToolOptions } from "../agents/tools/system-agent-tool.js";
import { getRuntimeConfig } from "../config/config.js";
import type { AetherConfig } from "../config/types.aether.js";
import { formatErrorMessage } from "../infra/errors.js";
import {
  AETHER_TOOLS_MCP_AGENT_SESSION_KEY_ENV,
  resolveToolsMcpAgentSessionKey,
  resolveToolsMcpAgentId,
  resolveToolsMcpSessionContext,
} from "./agent-session-env.js";
import {
  resolveAetherToolsMcpSystemAgentApproval,
  resolveAetherToolsMcpSystemAgentSurface,
  resolveAetherToolsMcpToolSelection,
  type AetherToolsMcpToolId,
} from "./aether-tools-serve-config.js";
import { connectToolsMcpServerToStdio, createToolsMcpServer } from "./tools-stdio-server.js";

export {
  AETHER_TOOLS_MCP_SYSTEM_AGENT_SURFACE_ENV,
  AETHER_TOOLS_MCP_TOOLS_ENV,
} from "./aether-tools-serve-config.js";

export { AETHER_TOOLS_MCP_AGENT_SESSION_KEY_ENV } from "./agent-session-env.js";

export function resolveAetherToolsMcpAgentSessionKey(
  env: NodeJS.ProcessEnv = process.env,
): string | undefined {
  return resolveToolsMcpAgentSessionKey(env);
}

export function resolveAetherToolsForMcp(
  params: {
    agentSessionKey?: string;
    agentId?: string;
    tools?: AetherToolsMcpToolId[];
    systemAgentSurface?: SystemAgentToolOptions["surface"];
    config?: AetherConfig;
  } = {},
): AnyAgentTool[] {
  const selection = params.tools ?? resolveAetherToolsMcpToolSelection();
  return selection.map((tool) => {
    if (tool === "aether") {
      return createSystemAgentTool({
        agentId: params.agentId,
        surface: params.systemAgentSurface ?? resolveAetherToolsMcpSystemAgentSurface(),
        ...resolveAetherToolsMcpSystemAgentApproval(),
      });
    }
    const agentSessionKey = (
      params.agentSessionKey ?? resolveAetherToolsMcpAgentSessionKey()
    )?.trim();
    if (!agentSessionKey) {
      throw new Error(`${AETHER_TOOLS_MCP_AGENT_SESSION_KEY_ENV} is required`);
    }
    const context = resolveToolsMcpSessionContext({ agentSessionKey, agentId: params.agentId });
    return createCronTool({
      agentSessionKey,
      agentId: context.agentId,
      // Same host-config resolution as plugin-tools-serve: the advertised cron
      // surface must reflect this deployment's cron.triggers.enabled gate.
      config: params.config ?? getRuntimeConfig(),
      creatorToolAllowlist: [{ name: AUTOMATIONS_TOOL_NAME }],
    });
  });
}

function createAetherToolsMcpServer(
  params: {
    tools?: AnyAgentTool[];
  } = {},
): Server {
  const tools = params.tools ?? resolveAetherToolsForMcp();
  return createToolsMcpServer({ name: "aether-tools", tools });
}

async function serveAetherToolsMcp(): Promise<void> {
  const server = createAetherToolsMcpServer({
    tools: resolveAetherToolsForMcp({ agentId: resolveToolsMcpAgentId() }),
  });
  await connectToolsMcpServerToStdio(server);
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  serveAetherToolsMcp().catch((err: unknown) => {
    process.stderr.write(`aether-tools-serve: ${formatErrorMessage(err)}\n`);
    process.exit(1);
  });
}
