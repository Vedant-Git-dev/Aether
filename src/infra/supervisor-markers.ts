// Defines process supervisor marker labels for gateway diagnostics.
import { GATEWAY_LAUNCH_AGENT_LABEL, resolveGatewayLaunchAgentLabel } from "../daemon/constants.js";
import { isGatewayExternallySupervised } from "./gateway-supervision.js";

const SUPERVISOR_HINTS = {
  launchd: ["AETHER_LAUNCHD_LABEL"],
  systemd: ["AETHER_SYSTEMD_UNIT", "INVOCATION_ID", "SYSTEMD_EXEC_PID", "JOURNAL_STREAM"],
  schtasks: ["AETHER_WINDOWS_TASK_NAME"],
} as const;

/** Environment keys that imply the gateway process is supervised by an external respawner. */
export const SUPERVISOR_HINT_ENV_VARS = [
  "AETHER_SUPERVISOR_MODE",
  "LAUNCH_JOB_LABEL",
  "LAUNCH_JOB_NAME",
  "XPC_SERVICE_NAME",
  ...SUPERVISOR_HINTS.launchd,
  ...SUPERVISOR_HINTS.systemd,
  ...SUPERVISOR_HINTS.schtasks,
  "AETHER_SERVICE_MARKER",
  "AETHER_SERVICE_KIND",
] as const;

/** Supported supervisor families that can respawn the gateway after update/restart handoff. */
export type RespawnSupervisor = "launchd" | "systemd" | "schtasks";
type GatewayRespawnSupervisor = RespawnSupervisor | "external";

interface DetectRespawnSupervisorOptions {
  includeLinuxAetherGatewayServiceMarker?: boolean;
}

function hasAnyHint(env: NodeJS.ProcessEnv, keys: readonly string[]): boolean {
  return keys.some((key) => {
    const value = env[key];
    return typeof value === "string" && value.trim().length > 0;
  });
}

function hasAetherGatewayServiceMarker(env: NodeJS.ProcessEnv): boolean {
  return (
    env.AETHER_SERVICE_MARKER?.trim() === "aether" &&
    env.AETHER_SERVICE_KIND?.trim() === "gateway"
  );
}

function isCurrentGatewayLaunchdJob(env: NodeJS.ProcessEnv): boolean {
  const expectedLabel = resolveGatewayLaunchAgentLabel(env.AETHER_PROFILE);
  if (
    [env.LAUNCH_JOB_LABEL, env.LAUNCH_JOB_NAME].some((value) => value?.trim() === expectedLabel)
  ) {
    return true;
  }
  return env.XPC_SERVICE_NAME?.trim() === GATEWAY_LAUNCH_AGENT_LABEL;
}

/** Detects the current platform supervisor from process environment hints. */
export function detectRespawnSupervisor(
  env: NodeJS.ProcessEnv = process.env,
  platform: NodeJS.Platform = process.platform,
  options: DetectRespawnSupervisorOptions = {},
): RespawnSupervisor | null {
  if (platform === "darwin") {
    return hasAnyHint(env, SUPERVISOR_HINTS.launchd) || isCurrentGatewayLaunchdJob(env)
      ? "launchd"
      : null;
  }
  if (platform === "linux") {
    return hasAnyHint(env, SUPERVISOR_HINTS.systemd) ||
      (options.includeLinuxAetherGatewayServiceMarker === true &&
        hasAetherGatewayServiceMarker(env))
      ? "systemd"
      : null;
  }
  if (platform === "win32") {
    if (hasAnyHint(env, SUPERVISOR_HINTS.schtasks)) {
      return "schtasks";
    }
    return hasAetherGatewayServiceMarker(env) ? "schtasks" : null;
  }
  return null;
}

/** Resolves gateway restart ownership without treating external mode as a native service manager. */
export function detectGatewayRespawnSupervisor(
  env: NodeJS.ProcessEnv = process.env,
  platform: NodeJS.Platform = process.platform,
  options: DetectRespawnSupervisorOptions = {},
): GatewayRespawnSupervisor | null {
  if (isGatewayExternallySupervised(env)) {
    return "external";
  }
  return detectRespawnSupervisor(env, platform, options);
}
