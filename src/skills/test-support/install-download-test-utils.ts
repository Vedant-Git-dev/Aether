// Install download test utilities provide isolated state and workspace paths.
import {
  createAetherTestState,
  type AetherTestState,
} from "../../test-utils/aether-test-state.js";

/** Creates isolated Aether state for install download tests. */
export async function createInstallDownloadTestState(): Promise<AetherTestState> {
  return await createAetherTestState({
    layout: "state-only",
    prefix: "aether-skills-install-",
  });
}
