import { truncateUtf16Safe } from "@aether/normalization-core/utf16-slice";
import { resolveStateDir } from "../../config/paths.js";
import { isContainerEnvironment } from "../../infra/container-environment.js";
import type { UpdateRunResult } from "../../infra/update-runner.js";
import { formatCliCommand } from "../command-format.js";

type UnsafeUpdateRecovery = Extract<
  NonNullable<UpdateRunResult["recovery"]>,
  { serviceRestartSafe: false }
>;

export function resolveUnsafeUpdateRecoveryGuidance(
  reason?: UnsafeUpdateRecovery["reason"],
  env: NodeJS.ProcessEnv = process.env,
): string {
  const triageCommand = formatCliCommand("aether triage", env);
  const guidance = `Run \`${triageCommand}\` on this machine to open a coding agent that can diagnose and repair the installation.`;
  if (reason === "state-migration-started") {
    return `${guidance} Candidate Doctor may have migrated state; keep the candidate installed and do not roll back code alone.`;
  }
  return guidance;
}

export function resolveUpdateResultNextAction(params: {
  result: UpdateRunResult;
  restart?: boolean;
  serviceRunning?: boolean;
  runningVersion?: string;
  verificationFailure?: string;
  env: NodeJS.ProcessEnv;
}): string | undefined {
  const { result, env } = params;
  if (result.status === "error") {
    if (result.reason === "rollback-project-changed") {
      return `Other global packages changed after staging; automatic rollback was refused to preserve them. Keep the candidate installed if its gateway is reachable; otherwise keep the gateway stopped. ${resolveUnsafeUpdateRecoveryGuidance(undefined, env)}`;
    }
    const reason =
      result.recovery?.serviceRestartSafe === false ? result.recovery.reason : undefined;
    const failure = truncateUtf16Safe(
      params.verificationFailure ?? result.reason ?? reason ?? "",
      240,
    );
    const runningVersion = truncateUtf16Safe(params.runningVersion ?? "", 120);
    const state = reason
      ? params.serviceRunning === true
        ? `The gateway is running${runningVersion ? ` ${runningVersion}` : ""} but did not pass verification (${failure}). `
        : `${params.serviceRunning === false ? "Managed gateway remains stopped because update recovery" : "Update recovery"} could not prove a runnable installation (${failure}). ${params.serviceRunning === false ? "Keep the gateway stopped until the update succeeds. " : ""}`
      : "";
    const configRefusal = result.steps.findLast(
      (step) => step.name === "config rollback",
    )?.stderrTail;
    const failedStep = result.steps.findLast((step) => step.exitCode !== 0 && !step.advisory);
    const containerPermissionFailure =
      (result.mode === "npm" || result.mode === "pnpm" || result.mode === "bun") &&
      failedStep !== undefined &&
      (failedStep.name.startsWith("global update") ||
        failedStep.name.startsWith("global install")) &&
      /\beacces\b/i.test(failedStep.stderrTail ?? "") &&
      isContainerEnvironment();
    // Record deployment-specific advice here so CLI output and later reports agree.
    // Keep the recovery constraints: an image change must not roll back migrated state.
    const deployment = containerPermissionFailure
      ? "Detected package update permission failure (EACCES) inside a container. Pull or build an Aether image with the target version, then recreate or redeploy the container with the same state/config mounts. In-container package changes are not durable. "
      : "";
    return `${configRefusal ? `${configRefusal} ` : ""}${state}${deployment}${resolveUnsafeUpdateRecoveryGuidance(reason, env)}`;
  }
  const command = (value: string) => formatCliCommand(value, env);
  if (result.reason === "dirty") {
    return `Git-based updates need a clean working tree before they can switch commits, fetch, or rebase. Commit, stash, or discard the local changes, then rerun \`${command("aether update")}\`.`;
  }
  if (result.reason === "not-git-install") {
    return `This Aether install isn't a git checkout, and the package manager couldn't be detected. Update via your package manager, then run \`${command("aether vitals")}\` and \`${command("aether gateway restart")}\`. Examples: \`npm i -g aether@latest\` or \`pnpm add -g aether@latest\`.`;
  }
  if (result.status === "ok") {
    if (params.restart === false && result.postUpdate?.plugins?.changed) {
      return `Plugins updated; Gateway restart skipped (--no-restart). Run \`${command("aether gateway restart")}\` to activate them in the running Gateway.`;
    }
    return `After verifying your history, preview recovery rollback retirement with ${command("aether update cleanup --dry-run")} for state ${resolveStateDir(env)}. Keep the same state/config overrides.`;
  }
  return undefined;
}
