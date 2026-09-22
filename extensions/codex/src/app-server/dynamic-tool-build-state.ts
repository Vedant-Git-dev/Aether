type AetherCodingToolsFactory =
  (typeof import("aether/plugin-sdk/agent-harness"))["createAetherCodingTools"];

/** Mutable dependency seam shared by dynamic-tool construction and its behavioral tests. */
export const dynamicToolBuildState: {
  aetherCodingToolsFactory?: AetherCodingToolsFactory;
} = {};
