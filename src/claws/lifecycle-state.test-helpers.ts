import { loadConfig } from "../config/config.js";
import type { AetherConfig } from "../config/types.aether.js";
import type { AetherTestState } from "../test-utils/aether-test-state.js";
import { applyClawAddPlan } from "./add.js";
import { buildClawRemovalFixture } from "./lifecycle-remove.test-support.js";

export function createClawRemoveTestFixtures(
  tempDirs: { make: (prefix: string) => string },
  getState: () => AetherTestState,
) {
  async function fixture(params: Parameters<typeof buildClawRemovalFixture>[1] = {}) {
    const current = await buildClawRemovalFixture(tempDirs.make("aether-claw-remove-"), params);
    return { ...current, env: { AETHER_STATE_DIR: getState().stateDir } };
  }

  async function addFixture(params: Parameters<typeof fixture>[0] = {}) {
    const current = await fixture(params);
    let config: AetherConfig = {};
    await applyClawAddPlan(current.plan, {
      consentPlanIntegrity: current.plan.planIntegrity,
      env: current.env,
      commitConfig: async (transform) => {
        config = transform(config);
        await getState().writeConfig(config);
      },
      cronGateway: { add: async () => ({ id: "scheduler-daily" }) },
      ...(params.withMcp ? { installMcpServers: async () => [] } : {}),
    });
    return { ...current, getConfig: loadConfig };
  }

  return { fixture, addFixture };
}
