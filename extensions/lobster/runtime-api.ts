// Lobster API module exposes the plugin public contract.
export { definePluginEntry } from "aether/plugin-sdk/core";
export type {
  AnyAgentTool,
  AetherPluginApi,
  AetherPluginToolContext,
  AetherPluginToolFactory,
} from "aether/plugin-sdk/core";
export {
  applyWindowsSpawnProgramPolicy,
  materializeWindowsSpawnProgram,
  resolveWindowsSpawnProgramCandidate,
} from "aether/plugin-sdk/windows-spawn";
