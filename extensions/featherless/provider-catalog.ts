// Featherless provider catalog exposes the curated setup model.
import { buildManifestModelProviderConfig } from "aether/plugin-sdk/provider-catalog-shared";
import type { ModelProviderConfig } from "aether/plugin-sdk/provider-model-shared";
import manifest from "./aether.plugin.json" with { type: "json" };

export {
  FEATHERLESS_BASE_URL,
  FEATHERLESS_DEFAULT_MODEL_ID,
  FEATHERLESS_DYNAMIC_COMPAT,
  FEATHERLESS_DYNAMIC_CONTEXT_WINDOW,
  FEATHERLESS_DYNAMIC_MAX_TOKENS,
  isFeatherlessCatalogModelId,
} from "./models.js";

export function buildFeatherlessProvider(): ModelProviderConfig {
  return buildManifestModelProviderConfig({
    providerId: "featherless",
    catalog: manifest.modelCatalog.providers.featherless,
  });
}
