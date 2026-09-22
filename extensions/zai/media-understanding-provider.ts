// Zai provider module implements model/runtime integration.
import type { MediaUnderstandingProvider } from "aether/plugin-sdk/media-understanding";

export const zaiMediaUnderstandingProvider: MediaUnderstandingProvider = {
  id: "zai",
  capabilities: ["image"],
  defaultModels: { image: "glm-4.6v" },
  autoPriority: { image: 60 },
  describeImage: undefined,
  describeImages: undefined,
};
