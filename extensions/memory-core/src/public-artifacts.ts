// Memory Core plugin module implements public artifacts behavior.
import {
  listMemoryHostPublicArtifacts,
  type MemoryPluginPublicArtifact,
} from "aether/plugin-sdk/memory-host-core";
import type { AetherConfig } from "../api.js";

export async function listMemoryCorePublicArtifacts(params: {
  cfg: AetherConfig;
}): Promise<MemoryPluginPublicArtifact[]> {
  return await listMemoryHostPublicArtifacts(params);
}
