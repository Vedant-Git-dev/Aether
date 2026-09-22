/**
 * Browser test-support re-exports from shared plugin-sdk test fixtures.
 */
export {
  createCliRuntimeCapture,
  expectGeneratedTokenPersistedToGatewayAuth,
  type CliRuntimeCapture,
} from "aether/plugin-sdk/test-fixtures";
export { createTempHomeEnv, useAutoCleanupTempDirTracker } from "aether/plugin-sdk/test-env";
export { isLiveTestEnabled } from "aether/plugin-sdk/test-live";
export type { AetherConfig } from "aether/plugin-sdk/config-contracts";
