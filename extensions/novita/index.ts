// Novita plugin entrypoint registers its Aether integration.
import { readConfiguredProviderCatalogEntries } from "aether/plugin-sdk/provider-catalog-shared";
import { defineSingleProviderPluginEntry } from "aether/plugin-sdk/provider-entry";
import { buildProviderReplayFamilyHooks } from "aether/plugin-sdk/provider-model-shared";
import { buildProviderToolCompatFamilyHooks } from "aether/plugin-sdk/provider-tools";
import manifest from "./aether.plugin.json" with { type: "json" };

const PROVIDER_ID = "novita";

export default defineSingleProviderPluginEntry({
  id: PROVIDER_ID,
  name: "NovitaAI Provider",
  description: "Official Aether NovitaAI provider plugin",
  manifest,
  provider: {
    label: "NovitaAI",
    docsPath: "/providers/novita",
    aliases: ["novita-ai", "novitaai"],
    manifestAuth: {
      noteTitle: "NovitaAI",
      noteMessage: "Manage API keys at https://novita.ai/settings/key-management",
    },
    catalog: {
      discoveryMode: "strict",
      allowExplicitBaseUrl: true,
      liveModelDiscovery: true,
    },
    augmentModelCatalog: ({ config }) =>
      readConfiguredProviderCatalogEntries({
        config,
        providerId: PROVIDER_ID,
      }),
    ...buildProviderReplayFamilyHooks({
      family: "openai-compatible",
      dropReasoningFromHistory: false,
    }),
    ...buildProviderToolCompatFamilyHooks("openai"),
  },
});
