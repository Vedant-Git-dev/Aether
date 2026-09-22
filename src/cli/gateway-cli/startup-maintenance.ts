import { formatErrorMessage } from "../../infra/errors.js";
import { findStartupMaintenanceRequiredError } from "../../infra/startup-maintenance-required.js";
import { createSubsystemLogger } from "../../logging/subsystem.js";
import { defaultRuntime } from "../../runtime.js";
import { formatCliCommand } from "../command-format.js";

const gatewayLog = createSubsystemLogger("gateway");

export function resolveGatewayStartupMaintenanceReason(error: unknown) {
  return findStartupMaintenanceRequiredError(error)?.reason;
}

export async function handleGatewayStartupMaintenance(error: unknown): Promise<boolean> {
  const reason = resolveGatewayStartupMaintenanceReason(error);
  if (!reason) {
    return false;
  }
  const stop = `Stop the service with ${formatCliCommand("aether gateway stop")} (or its service owner), then`;
  const guidance =
    reason === "a newer Aether build"
      ? `${stop} restore your pre-update backup created with ${formatCliCommand("aether backup")}, then start it again with ${formatCliCommand("aether gateway start")}. See https://docs.aether.ai/install/updating#rollback.`
      : `${stop} run ${formatCliCommand("aether vitals --fix")}, then start it again with ${formatCliCommand("aether gateway start")}.`;
  let parked = false;
  try {
    // launchd ignores exit 78 under KeepAlive. Park without opening the database,
    // which may also be unavailable to the persisted crash-loop counter.
    const { parkCurrentLaunchAgentForMaintenance } = await import("../../daemon/launchd.js");
    parked = await parkCurrentLaunchAgentForMaintenance();
  } catch (parkError) {
    gatewayLog.error(`failed to park the managed LaunchAgent: ${formatErrorMessage(parkError)}`);
  }
  gatewayLog.error(
    `gateway requires ${reason}${parked ? "; parked the managed LaunchAgent" : ""}. ${guidance}`,
  );
  defaultRuntime.error(`Gateway failed to start: ${formatErrorMessage(error)}. ${guidance}`);
  // systemd's RestartPreventExitStatus already treats EX_CONFIG as terminal.
  defaultRuntime.exit(78);
  return true;
}
