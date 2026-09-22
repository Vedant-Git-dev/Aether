// Declares extension points for agent session type augmentation.
export type AetherAgentSessionSkillSourceAugmentation = never;

declare module "aether/plugin-sdk/agent-sessions" {
  interface Skill {
    // Aether relies on the source identifier returned by skill loaders.
    source: string;
  }
}
