import type { PluginManifestRecord } from "../../plugins/manifest-registry.js";

export function createManifestRecord(
  id: string,
  overrides: Partial<PluginManifestRecord>,
): PluginManifestRecord {
  const rootDir = `/fixtures/${id}`;
  return {
    id,
    origin: "bundled",
    channels: [],
    providers: [],
    cliBackends: [],
    skills: [],
    hooks: [],
    rootDir,
    source: `${rootDir}/index.ts`,
    manifestPath: `${rootDir}/aether.plugin.json`,
    ...overrides,
  };
}
