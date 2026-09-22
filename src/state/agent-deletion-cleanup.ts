import { AsyncLocalStorage } from "node:async_hooks";
import path from "node:path";
import { err, ok, type Result } from "@aether/normalization-core/result";
import { normalizeAgentId } from "../routing/session-key.js";
import { resolveGlobalSingleton } from "../shared/global-singleton.js";
import type {
  AetherAgentDatabase,
  AetherAgentDatabaseOptions,
} from "./aether-agent-db-contract.js";
import { resolveAetherAgentSqlitePath } from "./aether-agent-db.paths.js";
import { resolveAetherStateSqlitePath } from "./aether-state-db.paths.js";

type AgentDeletionCleanupRow = {
  agentId: string;
  operationId: string;
  cleanupCompleted: boolean;
};

type AgentDeletionDatabaseCleanupScope = {
  agentId: string;
  path: string;
  statePath: string;
  assertCurrent: () => void;
  assertJournal: (statePath: string, entries: readonly AgentDeletionCleanupRow[]) => string;
  registerClose: (close: () => void) => void;
  retryClose: () => void;
  withCommit: (commit: () => void) => void;
};

const databaseCleanup = resolveGlobalSingleton(
  Symbol.for("aether.agentDeletionDatabaseCleanup"),
  () => new AsyncLocalStorage<AgentDeletionDatabaseCleanupScope>(),
);
const cleanupHandles = resolveGlobalSingleton(
  Symbol.for("aether.agentDeletionDatabaseCleanupHandles"),
  () => new Map<AetherAgentDatabase, AgentDeletionDatabaseCleanupScope>(),
);

/** The lifecycle owner supplies live closures, never a transferable operation id. */
export function createAgentDeletionDatabaseCleanup(owner: {
  statePath: string;
  assertAdmission: () => void;
  withCommit: (commit: () => void) => void;
  assertCurrent: () => void;
  assertJournal: (statePath: string, entries: readonly AgentDeletionCleanupRow[]) => string;
}) {
  return async <T>(
    target: { agentId: string; path: string },
    run: () => Promise<T>,
  ): Promise<T> => {
    let active = true;
    const closers = new Set<() => void>();
    const closeHandles = () => {
      const errors: unknown[] = [];
      for (const close of [...closers].toReversed()) {
        try {
          close();
          closers.delete(close);
        } catch (error) {
          errors.push(error);
        }
      }
      return errors;
    };
    const assertActive = () => {
      if (!active) {
        throw new Error("Agent deletion database cleanup is no longer active.");
      }
    };
    const scope: AgentDeletionDatabaseCleanupScope = {
      agentId: normalizeAgentId(target.agentId),
      path: path.resolve(target.path),
      statePath: path.resolve(owner.statePath),
      assertCurrent: () => {
        assertActive();
        owner.assertCurrent();
      },
      assertJournal: (statePath, entries) => {
        assertActive();
        return owner.assertJournal(statePath, entries);
      },
      registerClose: (close) => {
        assertActive();
        closers.add(close);
      },
      retryClose: () => {
        if (active) {
          throw new Error("Agent database belongs to an active deletion cleanup.");
        }
        const errors = closeHandles();
        if (errors.length > 0) {
          throw new AggregateError(errors, "Agent deletion database close retry failed.");
        }
      },
      withCommit: (commit) => {
        assertActive();
        owner.withCommit(commit);
      },
    };
    return await databaseCleanup.run(scope, async () => {
      let outcome: Result<T, unknown>;
      const closeErrors: unknown[] = [];
      try {
        scope.assertCurrent();
        // A failed cold close keeps its tag and native lease. A fresh exact owner
        // retries only that settled close; it never adopts the expired write scope.
        for (const previous of new Set(cleanupHandles.values())) {
          if (
            previous.statePath === scope.statePath &&
            previous.agentId === scope.agentId &&
            previous.path === scope.path
          ) {
            previous.retryClose();
          }
        }
        owner.assertAdmission();
        const value = await run();
        scope.assertCurrent();
        outcome = ok(value);
      } catch (error) {
        outcome = err(error);
      } finally {
        closeErrors.push(...closeHandles());
        // Retained async callbacks keep this same object and must fail after settlement.
        // A failed native close remains tagged and leased for the existing close retry.
        active = false;
      }
      if (!outcome.ok) {
        throw closeErrors.length > 0
          ? new AggregateError(
              [outcome.error, ...closeErrors],
              "Agent deletion database cleanup failed.",
            )
          : outcome.error;
      }
      if (closeErrors.length > 0) {
        throw closeErrors.length === 1
          ? closeErrors[0]
          : new AggregateError(closeErrors, "Agent deletion database cleanup failed.");
      }
      return outcome.value;
    });
  };
}

export function getAgentDeletionDatabaseCleanup(
  params: AetherAgentDatabaseOptions & { statePath?: string },
): AgentDeletionDatabaseCleanupScope | undefined {
  const scope = databaseCleanup.getStore();
  if (
    !scope ||
    scope.agentId !== normalizeAgentId(params.agentId) ||
    scope.path !== resolveAetherAgentSqlitePath(params)
  ) {
    return undefined;
  }
  const statePath = params.statePath ?? resolveAetherStateSqlitePath(params.env ?? process.env);
  if (scope.statePath !== path.resolve(statePath)) {
    throw new Error("Agent deletion database cleanup belongs to another state database.");
  }
  return scope;
}

export function assertAgentDeletionDatabaseCleanupAccess(
  database: AetherAgentDatabase,
  options: AetherAgentDatabaseOptions,
): void {
  const scope = getAgentDeletionDatabaseCleanup(options);
  const owner = cleanupHandles.get(database);
  if (owner && owner !== scope) {
    throw new Error("Agent database belongs to an active deletion cleanup.");
  }
  scope?.assertCurrent();
}

export function assertAgentDeletionCleanupAliases(
  options: AetherAgentDatabaseOptions,
  isSamePath: (left: string, right: string) => boolean,
): void {
  // Only cleanup-held files need this rare physical alias check on a cache miss.
  const pathname = resolveAetherAgentSqlitePath(options);
  for (const owned of cleanupHandles.keys()) {
    if (isSamePath(owned.path, pathname)) {
      assertAgentDeletionDatabaseCleanupAccess(owned, options);
    }
  }
}

export function registerAgentDeletionDatabaseCleanup(
  database: AetherAgentDatabase,
  options: AetherAgentDatabaseOptions,
): AgentDeletionDatabaseCleanupScope | undefined {
  const scope = getAgentDeletionDatabaseCleanup(options);
  scope?.assertCurrent();
  if (scope) {
    cleanupHandles.set(database, scope);
  }
  return scope;
}

/** Release the tag only after the native owner has closed and released its lease. */
export function releaseAgentDeletionDatabaseCleanup(database: AetherAgentDatabase): void {
  cleanupHandles.delete(database);
}
