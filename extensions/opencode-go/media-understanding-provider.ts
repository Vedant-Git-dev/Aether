// Opencode Go provider module implements model/runtime integration.
import type { MediaUnderstandingProvider } from "aether/plugin-sdk/media-understanding";

export const opencodeGoMediaUnderstandingProvider: MediaUnderstandingProvider = {
  id: "opencode-go",
  capabilities: ["image"],
  defaultModels: {
    image: "kimi-k2.6",
  },
  describeImage: undefined,
  describeImages: undefined,
};
