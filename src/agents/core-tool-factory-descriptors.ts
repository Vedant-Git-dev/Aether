/**
 * Static identity for names that select core agent factory families before assembly.
 */

import { AUTOMATIONS_TOOL_NAME } from "./tools/automations-tool-name.js";

export type CoreToolFactoryFamily = "base-coding" | "shell" | "aether";

type CoreToolFactoryDescriptor = {
  readonly name: string;
  readonly family: CoreToolFactoryFamily;
};

const CORE_TOOL_FACTORY_DESCRIPTORS = [
  { name: "edit", family: "base-coding" },
  { name: "read", family: "base-coding" },
  { name: "ls", family: "base-coding" },
  { name: "write", family: "base-coding" },
  { name: "apply_patch", family: "shell" },
  { name: "exec", family: "shell" },
  { name: "process", family: "shell" },
  { name: "agents_list", family: "aether" },
  // Static factory identity only; runtime and tools.catalog apply the Swarm config gate.
  { name: "agents_wait", family: "aether" },
  { name: "ask_user", family: "aether" },
  { name: "aether", family: "aether" },
  { name: "computer", family: "aether" },
  { name: "conversations_list", family: "aether" },
  { name: "conversations_send", family: "aether" },
  { name: "conversations_turn", family: "aether" },
  { name: AUTOMATIONS_TOOL_NAME, family: "aether" },
  { name: "screen", family: "aether" },
  { name: "secrets", family: "aether" },
  { name: "dashboard", family: "aether" },
  { name: "gateway", family: "aether" },
  { name: "get_goal", family: "aether" },
  { name: "github_identity_status", family: "aether" },
  { name: "github_publish", family: "aether" },
  { name: "heartbeat_respond", family: "aether" },
  { name: "view_image", family: "aether" },
  { name: "image_generate", family: "aether" },
  { name: "message", family: "aether" },
  { name: "music_generate", family: "aether" },
  { name: "nodes", family: "aether" },
  { name: "pdf", family: "aether" },
  { name: "session_status", family: "aether" },
  { name: "show_widget", family: "aether" },
  { name: "progress_card", family: "aether" },
  { name: "sessions", family: "aether" },
  { name: "sessions_history", family: "aether" },
  { name: "sessions_list", family: "aether" },
  { name: "sessions_search", family: "aether" },
  { name: "sessions_send", family: "aether" },
  { name: "sessions_spawn", family: "aether" },
  { name: "sessions_yield", family: "aether" },
  { name: "structured_output", family: "aether" },
  { name: "skill_workshop", family: "aether" },
  { name: "suggest_task", family: "aether" },
  { name: "create_goal", family: "aether" },
  { name: "subagents", family: "aether" },
  { name: "terminal", family: "aether" },
  { name: "portal", family: "aether" },
  { name: "transcripts", family: "aether" },
  { name: "tts", family: "aether" },
  { name: "update_goal", family: "aether" },
  { name: "dismiss_task", family: "aether" },
  { name: "video_generate", family: "aether" },
  { name: "web_fetch", family: "aether" },
  { name: "web_search", family: "aether" },
] as const satisfies readonly CoreToolFactoryDescriptor[];

const CORE_TOOL_FACTORY_FAMILY_BY_NAME = new Map<string, CoreToolFactoryFamily>(
  CORE_TOOL_FACTORY_DESCRIPTORS.map(({ name, family }) => [name, family]),
);

export type AetherCodingToolConstructionPlan = {
  includeBaseCodingTools: boolean;
  includeShellTools: boolean;
  includeChannelTools: boolean;
  includeAetherTools: boolean;
  includePluginTools: boolean;
};

export function resolveCoreToolFactoryFamily(name: string): CoreToolFactoryFamily | undefined {
  return CORE_TOOL_FACTORY_FAMILY_BY_NAME.get(name);
}

export function listCoreToolFactoryDescriptors(): readonly CoreToolFactoryDescriptor[] {
  return CORE_TOOL_FACTORY_DESCRIPTORS;
}

/**
 * Core coding primitives (file + shell families). Tool-search compaction keeps
 * these directly visible: hiding them behind search adds a lookup round-trip to
 * nearly every coding turn.
 */
export function isCoreCodingSurfaceToolName(name: string): boolean {
  const family = CORE_TOOL_FACTORY_FAMILY_BY_NAME.get(name);
  return family === "base-coding" || family === "shell";
}
