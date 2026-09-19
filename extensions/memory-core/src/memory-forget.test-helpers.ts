import fs from "node:fs/promises";
import path from "node:path";
import type { AetherConfig } from "aether/plugin-sdk/memory-core-host-engine-foundation";
import { resetPluginStateStoreForTests } from "aether/plugin-sdk/plugin-state-test-runtime";
import { upsertSessionEntry } from "aether/plugin-sdk/session-store-runtime";
import { openAetherAgentDatabase } from "aether/plugin-sdk/sqlite-runtime";
import {
  closeAetherAgentDatabasesForTest,
  closeAetherStateDatabaseForTest,
} from "aether/plugin-sdk/sqlite-runtime-testing";
import { vi } from "vitest";
import { configureMemoryCoreDreamingStateForTests } from "./test-helpers.js";

export async function createMemoryForgetFixture(stateDir: string) {
  const workspaceDir = path.join(stateDir, "workspace");
  await fs.mkdir(workspaceDir);
  vi.stubEnv("AETHER_STATE_DIR", stateDir);
  await configureMemoryCoreDreamingStateForTests();
  const cfg: AetherConfig = {
    agents: { defaults: { workspace: workspaceDir }, list: [{ id: "main", default: true }] },
  };
  return { stateDir, workspaceDir, cfg };
}

export function closeMemoryForgetFixture(): void {
  closeAetherAgentDatabasesForTest();
  closeAetherStateDatabaseForTest();
  resetPluginStateStoreForTests();
  vi.unstubAllEnvs();
}

export async function seedMemoryForgetSession(
  sessionId: string,
  hookSource?: "gmail" | "webhook",
): Promise<void> {
  const sessionKey = `agent:main:${sessionId}`;
  await upsertSessionEntry({
    agentId: "main",
    sessionKey,
    entry: { sessionId, updatedAt: 1_000 },
  });
  if (hookSource) {
    openAetherAgentDatabase({ agentId: "main" })
      .db.prepare(
        "UPDATE session_windows SET hook_external_content_source = ? WHERE session_id = ?",
      )
      .run(hookSource, sessionId);
  }
}
