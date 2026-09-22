// Together provider module implements model/runtime integration.
import { buildManifestModelProviderConfig } from "aether/plugin-sdk/provider-catalog-shared";
import type { ModelProviderConfig } from "aether/plugin-sdk/provider-model-shared";
import manifest from "./aether.plugin.json" with { type: "json" };

export function buildTogetherProvider(): ModelProviderConfig {
  return buildManifestModelProviderConfig({
    providerId: "together",
    catalog: manifest.modelCatalog.providers.together,
  });
}
