// Mattermost plugin module implements secret input behavior.
export {
  buildSecretInputSchema,
  hasConfiguredSecretInput,
  resolveSecretInputString,
} from "aether/plugin-sdk/secret-input";
export type { SecretInputStringResolutionMode } from "aether/plugin-sdk/secret-input";
