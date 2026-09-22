import type { SqliteSchemaIssue } from "../infra/sqlite-schema-contract.js";
import type { AetherExternalStateOwnership } from "./aether-state-ownership.js";

export type IncompatibleAetherDatabase = {
  kind: "agent" | "state";
  path: string;
  agentId?: string;
  foundVersion: number;
  supportedVersion: number;
  writerAppVersion?: string;
};

export type IndeterminateAetherDatabase = {
  kind: "agent" | "state";
  path: string;
  reason: string;
};

export type DeferredStateSchemaPublication = {
  kind: "state";
  path: string;
  foundVersion: number;
  contentVersion: number;
  runId?: string;
  publishAfterMs?: number | null;
  message: string;
};

export type AetherDatabaseSchemaPreflight = {
  incompatible: IncompatibleAetherDatabase[];
  indeterminate: IndeterminateAetherDatabase[];
  pendingMigrations?: Omit<IncompatibleAetherDatabase, "writerAppVersion">[];
  deferredSchemaPublications?: DeferredStateSchemaPublication[];
};

export type AetherStateSchemaPreflightResult = {
  databasePath: string;
  foundVersion: number | null;
  contentVersion?: number;
  deferredPublication?: DeferredStateSchemaPublication;
  issues: SqliteSchemaIssue[];
  ownership: AetherExternalStateOwnership | null;
  reason?: string;
  requiresWrite: boolean;
  schema: "aether.state-schema-preflight.v1";
  status: "exact" | "startup-repairable" | "migration-required" | "incompatible" | "indeterminate";
  targetVersion: number;
};

export type AetherAgentSchemaPreflightResult = Omit<
  AetherStateSchemaPreflightResult,
  "schema" | "ownership" | "status"
> & {
  schema: "aether.agent-schema-preflight.v1";
  agentId: string;
  status: "exact" | "incompatible" | "indeterminate";
};
