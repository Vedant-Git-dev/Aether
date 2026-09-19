// Moonshot API module exposes the plugin public contract.
import type { ProviderPlugin } from "aether/plugin-sdk/provider-model-shared";
import manifest from "./aether.plugin.json" with { type: "json" };

const noopAuth = async () => ({ profiles: [] });

export function createMoonshotProvider(): ProviderPlugin {
  return {
    id: "moonshot",
    label: "Moonshot",
    docsPath: "/providers/moonshot",
    aliases: ["moonshotai", "moonshot-ai"],
    auth: manifest.providerAuthChoices.map((choice) => ({
      id: choice.method,
      kind: "api_key",
      label: choice.choiceLabel,
      hint: choice.groupHint,
      run: noopAuth,
      wizard: { groupLabel: choice.groupLabel },
    })),
  };
}
