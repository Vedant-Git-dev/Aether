// Voyage plugin entrypoint registers its Aether integration.
import { definePluginEntry } from "aether/plugin-sdk/plugin-entry";
import { voyageMemoryEmbeddingProviderAdapter } from "./memory-embedding-adapter.js";

export default definePluginEntry({
  id: "voyage",
  name: "Voyage Embeddings",
  description: "Voyage memory embedding provider plugin",
  register(api) {
    api.registerEmbeddingProvider(voyageMemoryEmbeddingProviderAdapter);
  },
});
