import { statSync } from "node:fs";
import type { DatabaseSync } from "node:sqlite";
import { resolveGlobalSingleton } from "../shared/global-singleton.js";

type AgentDatabaseOwner = { db: DatabaseSync };
export type AetherAgentDatabaseIdentity = string | symbol;

const identities = resolveGlobalSingleton(
  Symbol.for("aether.agentDatabaseIdentities"),
  () => new WeakMap<DatabaseSync, { identity: AetherAgentDatabaseIdentity; filename: string }>(),
);

/** Prepare physical identity once at open; cached aliases must never be resolved again. */
export function registerAetherAgentDatabaseIdentity(db: DatabaseSync): void {
  const filename = db.location() ?? "";
  const file = filename ? statSync(filename, { bigint: true }) : undefined;
  const identity = file ? `${file.dev}:${file.ino}` : Symbol("incognito-agent-database");
  identities.set(db, { identity, filename });
}

/** Reuse facts captured at open; aliases must never be resolved again at a handoff. */
export function readAetherAgentDatabaseIdentity(database: AgentDatabaseOwner) {
  const prepared = identities.get(database.db);
  if (prepared === undefined) {
    throw new Error("Aether agent database identity was not prepared at open");
  }
  return prepared;
}

export type AetherAgentDatabaseClaim = {
  database: AgentDatabaseOwner & { agentId: string; path: string };
  identity: AetherAgentDatabaseIdentity;
  isCurrent: () => boolean;
  assertCurrent: () => void;
  release: () => void;
};

export function createAetherAgentDatabaseClaim(
  database: AetherAgentDatabaseClaim["database"],
  release: () => void,
): AetherAgentDatabaseClaim {
  let released = false;
  const isCurrent = () => !released && database.db.isOpen;
  return {
    database,
    identity: readAetherAgentDatabaseIdentity(database).identity,
    isCurrent,
    assertCurrent: () => {
      if (!isCurrent()) {
        throw new Error("Aether agent database claim is no longer current");
      }
    },
    release: () => {
      if (!released) {
        released = true;
        release();
      }
    },
  };
}
