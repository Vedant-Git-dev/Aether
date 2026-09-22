// Kilocode plugin entrypoint registers its Aether integration.
import { readConfiguredProviderCatalogEntries } from "aether/plugin-sdk/provider-catalog-shared";
import { defineSingleProviderPluginEntry } from "aether/plugin-sdk/provider-entry";
import { buildProviderReplayFamilyHooks } from "aether/plugin-sdk/provider-model-shared";
import { applyKilocodeConfig, KILOCODE_DEFAULT_MODEL_REF } from "./onboard.js";
import manifest from "./aether.plugin.json" with { type: "json" };
import { buildKilocodeProvider, buildKilocodeProviderWithDiscovery } from "./provider-catalog.js";
import { wrapKilocodeProviderStream } from "./stream.js";

const PROVIDER_ID = "kilocode";

export default defineSingleProviderPluginEntry({
  id: PROVIDER_ID,
  name: "Kilo Gateway Provider",
  description: "Bundled Kilo Gateway provider plugin",
  manifest,
  provider: {
    label: "Kilo Gateway",
    docsPath: "/providers/kilocode",
    manifestAuth: {
      defaultModel: KILOCODE_DEFAULT_MODEL_REF,
      applyConfig: applyKilocodeConfig,
    },
    catalog: {
      discoveryMode: "strict",
      buildProvider: () => buildKilocodeProviderWithDiscovery({ discoveryMode: "strict" }),
      buildStaticProvider: buildKilocodeProvider,
    },
    augmentModelCatalog: ({ config }) =>
      readConfiguredProviderCatalogEntries({
        config,
        providerId: PROVIDER_ID,
      }),
    ...buildProviderReplayFamilyHooks({ family: "passthrough-gemini" }),
    wrapStreamFn: wrapKilocodeProviderStream,
    isCacheTtlEligible: (ctx) => ctx.modelId.startsWith("anthropic/"),
  },
});
