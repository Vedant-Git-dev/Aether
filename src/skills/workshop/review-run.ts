import { prepareSystemAgentRunAdmission } from "../../agents/admitted-run-context.js";
import type { RunEmbeddedAgentParams } from "../../agents/embedded-agent-runner/run/params.js";
import type { AetherConfig } from "../../config/types.aether.js";
import { createBackgroundWorkOwner } from "../../process/background-work.js";
import { getGatewayRestartDrainSignal } from "../../process/gateway-work-admission.js";

const reviews = createBackgroundWorkOwner({ owner: "core:skill-workshop", maxConcurrent: 1 });

/** Experience reviews retain admission, model locking, and background capacity. */
export async function runSkillWorkshopReview(
  params: RunEmbeddedAgentParams & {
    agentId: string;
    config: AetherConfig;
  },
) {
  const restartSignal = getGatewayRestartDrainSignal();
  const abortSignal = params.abortSignal
    ? AbortSignal.any([restartSignal, params.abortSignal])
    : restartSignal;
  abortSignal.throwIfAborted();
  const preparedRunAdmission =
    params.preparedRunAdmission ??
    prepareSystemAgentRunAdmission(
      params.config,
      params.runId,
      params.agentId,
      "skill-workshop.experience",
    );
  try {
    const { runEmbeddedAgent } = await import("../../agents/embedded-agent.js");
    return await runEmbeddedAgent({
      ...params,
      preparedRunAdmission,
      abortSignal,
      lane: reviews.lane,
      agentHarnessId: "aether",
      agentHarnessRuntimeOverride: "aether",
      // Review prompts and cloned prefixes are sized for this exact model.
      modelSelectionLocked: true,
      modelFallbacksOverride: [],
      requestedRouteResolution: "resolved",
      disableTrajectory: true,
      skillWorkshopProposalOnly: params.skillWorkshopProposalOnly ?? true,
      cleanupBundleMcpOnRunEnd: true,
      verboseLevel: "off",
    });
  } finally {
    preparedRunAdmission.close();
  }
}
