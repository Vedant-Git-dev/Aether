import type { AetherStateDatabaseOptions } from "../state/aether-state-db-contract.js";
import { withExistingAetherStateDatabaseArtifactPreservingReadOnly } from "../state/aether-state-db-readonly.js";
import {
  isUpdateRecoveryPending,
  UpdateRecoveryRequiredError,
  type UpdateRecoveryRecord,
} from "./update-run-recovery-schema.js";
import { readRecoveries } from "./update-run-recovery-store.js";
export type { UpdateRecoveryFence, UpdateRecoveryHandoff } from "./update-run-recovery-types.js";
export { UpdateRecoveryRequiredError } from "./update-run-recovery-schema.js";
export type { UpdateRecoveryRecord } from "./update-run-recovery-schema.js";
export { inspectUpdateRecoveries } from "./update-run-recovery-store.js";
/** Must run before general database open, admission writes, or runtime migration. */
function loadUpdateRecoveries(options: AetherStateDatabaseOptions = {}): UpdateRecoveryRecord[] {
  return (
    withExistingAetherStateDatabaseArtifactPreservingReadOnly(
      ({ db }) => readRecoveries(db),
      options,
    ) ?? []
  );
}
export function loadUpdateRecovery(
  runId: string,
  options: AetherStateDatabaseOptions = {},
): UpdateRecoveryRecord | undefined {
  return loadUpdateRecoveries(options).find((record) => record.runId === runId);
}
/** Detection only. This delivery never claims, rewrites, or retires retained recovery. */
export function assertNoPendingUpdateRecovery(options: AetherStateDatabaseOptions = {}): void {
  const pending = loadUpdateRecoveries(options).find(isUpdateRecoveryPending);
  if (pending) {
    throw new UpdateRecoveryRequiredError(pending);
  }
}
