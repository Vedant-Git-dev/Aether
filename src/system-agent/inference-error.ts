import { truncateUtf16Safe } from "@aether/normalization-core/utf16-slice";
import { formatErrorMessage } from "../infra/errors.js";

type SystemAgentInferenceStage = "agent-turn" | "planner" | "conversation";

const INFERENCE_UNAVAILABLE_MESSAGE =
  "Aether could not reach working inference. Run `aether awaken` on the machine running Aether to reconnect — it live-tests the route before saving it. Then try again.";
const INFERENCE_FAILURE_SUMMARY_MAX_CHARS = 300;

function inferenceUnavailableMessage(failures: readonly unknown[]): string {
  const detail = failures.length > 0 ? formatErrorMessage(failures[0]).trim() : "";
  if (!detail) {
    return INFERENCE_UNAVAILABLE_MESSAGE;
  }
  const summary =
    detail.length > INFERENCE_FAILURE_SUMMARY_MAX_CHARS
      ? `${truncateUtf16Safe(detail, INFERENCE_FAILURE_SUMMARY_MAX_CHARS - 1)}…`
      : detail;
  return `${INFERENCE_UNAVAILABLE_MESSAGE} Cause: ${summary}`;
}

/** Safe public error for an Aether turn that could not complete with intelligence. */
export class SystemAgentInferenceUnavailableError extends Error {
  readonly code = "SYSTEM_AGENT_INFERENCE_UNAVAILABLE";

  constructor(
    readonly stage: SystemAgentInferenceStage,
    readonly failures: readonly unknown[] = [],
  ) {
    super(inferenceUnavailableMessage(failures));
    this.name = "SystemAgentInferenceUnavailableError";
  }
}

export function isSystemAgentInferenceUnavailableError(
  error: unknown,
): error is SystemAgentInferenceUnavailableError {
  return (
    error instanceof SystemAgentInferenceUnavailableError ||
    (error instanceof Error &&
      "code" in error &&
      error.code === "SYSTEM_AGENT_INFERENCE_UNAVAILABLE")
  );
}
