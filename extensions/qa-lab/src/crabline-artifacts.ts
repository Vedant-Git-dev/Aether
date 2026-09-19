// Qa Lab plugin module resolves Crabline artifact paths reported by completed generations.
import type { AetherCrablineChannelDriverSelection } from "@openclaw/crabline";

type QaCrablineChannelDriverArtifactPaths = {
  capabilityMatrixPath: string;
  providerReadinessArtifactPath: string;
};

export type QaSuiteChannelDriverSelection = Omit<
  AetherCrablineChannelDriverSelection,
  "capabilityMatrixPath" | "providerReadinessArtifactPath"
> &
  QaCrablineChannelDriverArtifactPaths;

function hasQaCrablineArtifactPath(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function readArtifactPath(value: unknown) {
  return hasQaCrablineArtifactPath(value) ? value.trim() : undefined;
}

export function resolveQaCrablineChannelDriverArtifactPaths(params: {
  result?: {
    capabilityMatrixPath?: unknown;
    providerReadinessArtifactPath?: unknown;
  };
  selection?: AetherCrablineChannelDriverSelection | null;
}): QaCrablineChannelDriverArtifactPaths | undefined {
  if (!params.selection) {
    return undefined;
  }
  return {
    capabilityMatrixPath:
      readArtifactPath(params.result?.capabilityMatrixPath) ??
      params.selection.capabilityMatrixPath,
    providerReadinessArtifactPath:
      readArtifactPath(params.result?.providerReadinessArtifactPath) ??
      params.selection.providerReadinessArtifactPath,
  };
}
