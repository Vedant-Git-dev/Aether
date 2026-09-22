// Kilocode provider module implements model/runtime integration.
import { buildManifestModelProviderConfig } from "aether/plugin-sdk/provider-catalog-shared";
import type { ModelProviderConfig } from "aether/plugin-sdk/provider-model-shared";
import manifest from "./aether.plugin.json" with { type: "json" };
import {
  discoverKilocodeModels,
  KILOCODE_BASE_URL as LOCAL_KILOCODE_BASE_URL,
} from "./provider-models.js";

export function buildKilocodeProvider(): ModelProviderConfig {
  return buildManifestModelProviderConfig({
    providerId: "kilocode",
    catalog: manifest.modelCatalog.providers.kilocode,
  });
}

export async function buildKilocodeProviderWithDiscovery(
  options: { discoveryMode?: "strict" } = {},
): Promise<ModelProviderConfig> {
  const models = await discoverKilocodeModels(options);
  return {
    baseUrl: LOCAL_KILOCODE_BASE_URL,
    api: "openai-completions",
    models,
  };
}
