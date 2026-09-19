// Links plugin peer packages for local development installs.
import fs from "node:fs/promises";
import path from "node:path";
import type { PluginInstallRecord } from "../config/types.plugins.js";
import { hasErrnoCode } from "../infra/errors.js";
import { resolveUserPath } from "../infra/home-dir.js";
import { readRootJsonObjectSync } from "../infra/json-files.js";
import { resolveAetherPackageRootSync } from "../infra/aether-root.js";
import { isPathInside } from "../infra/path-guards.js";
import { resolvePluginInstallDir } from "./install-paths.js";
import { listNpmPackageDirs } from "./npm-package-dirs.js";

type PluginPeerLinkLogger = {
  info?: (message: string) => void;
  warn?: (message: string) => void;
};

type RelinkManagedNpmRootResult = {
  checked: number;
  attempted: number;
  repaired: number;
  skipped: number;
};

export type AetherPeerLinkAuditIssue = {
  packageName: string;
  packageDir: string;
  reason: string;
};

type AuditManagedNpmRootResult = {
  checked: number;
  broken: number;
  issues: AetherPeerLinkAuditIssue[];
};

type AetherPeerLinkResult = "linked" | "skipped" | "unchanged";

type AetherHostDependency = {
  declaration: "peerDependencies" | "dependencies";
  spec: string;
};

type RegisteredAetherHostLinkResult = {
  checked: number;
  repaired: number;
  skipped: number;
  issues: AetherPeerLinkAuditIssue[];
};

/** Resolve the host declaration consistently for peer and direct runtime dependencies. */
export function resolveAetherHostDependency(manifest: {
  dependencies?: unknown;
  peerDependencies?: unknown;
}): AetherHostDependency | null {
  for (const declaration of ["peerDependencies", "dependencies"] as const) {
    const dependencies = manifest[declaration];
    const spec =
      typeof dependencies === "object" && dependencies !== null && !Array.isArray(dependencies)
        ? (dependencies as Record<string, unknown>).aether
        : undefined;
    if (typeof spec === "string" && spec) {
      return { declaration, spec };
    }
  }
  return null;
}

async function readSafePackageManifest(
  packageDir: string,
): Promise<Record<string, unknown> | null> {
  const result = readRootJsonObjectSync({
    rootDir: packageDir,
    relativePath: "package.json",
    boundaryLabel: "installed plugin package directory",
  });
  if (!result.ok) {
    if (
      result.reason === "open" &&
      (result.failure.error as NodeJS.ErrnoException | undefined)?.code === "ENOENT"
    ) {
      return null;
    }
    if (result.reason === "parse") {
      throw new SyntaxError(result.error);
    }
    if (result.reason === "open" && result.failure.error instanceof Error) {
      throw result.failure.error;
    }
    throw new Error(
      `Could not safely read package.json from ${packageDir}: ${
        result.reason === "open" ? result.failure.reason : result.error
      }`,
    );
  }
  return result.value;
}

async function readPackageAetherLinkDependencies(
  packageDir: string,
): Promise<Record<string, string>> {
  const manifest = await readSafePackageManifest(packageDir);
  const dependency = manifest ? resolveAetherHostDependency(manifest) : null;
  return dependency ? { aether: dependency.spec } : {};
}

async function listManagedNpmRootPackageDirs(npmRoot: string): Promise<string[]> {
  const packageDirs = await listNpmPackageDirs(npmRoot, {
    includeEntry: (entry, scoped) => entry.isDirectory() && (scoped || !entry.name.startsWith(".")),
  });
  return packageDirs.toSorted((a, b) => a.localeCompare(b));
}

async function safeRealpath(filePath: string): Promise<string | null> {
  try {
    return await fs.realpath(filePath);
  } catch {
    return null;
  }
}

function managedPackageNameFromDir(params: { npmRoot: string; packageDir: string }): string {
  return path
    .relative(path.join(params.npmRoot, "node_modules"), params.packageDir)
    .split(path.sep)
    .join("/");
}

async function auditAetherPeerDependency(params: {
  hostRoot: string;
  packageDir: string;
  npmRoot?: string;
  packageName?: string;
}): Promise<AetherPeerLinkAuditIssue | null> {
  const packageName =
    params.packageName ??
    (params.npmRoot
      ? managedPackageNameFromDir({
          npmRoot: params.npmRoot,
          packageDir: params.packageDir,
        })
      : path.basename(params.packageDir));
  const nodeModulesDir = path.join(params.packageDir, "node_modules");
  try {
    const existing = await fs.lstat(nodeModulesDir);
    if (!existing.isDirectory() || existing.isSymbolicLink()) {
      return {
        packageName,
        packageDir: params.packageDir,
        reason: `${nodeModulesDir} is not a real directory`,
      };
    }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") {
      return {
        packageName,
        packageDir: params.packageDir,
        reason: `missing ${path.join(nodeModulesDir, "aether")}`,
      };
    }
    throw error;
  }

  const linkPath = path.join(nodeModulesDir, "aether");
  const currentTarget = await safeRealpath(linkPath);
  if (!currentTarget) {
    return {
      packageName,
      packageDir: params.packageDir,
      reason: `missing ${linkPath}`,
    };
  }
  const expectedTarget = (await safeRealpath(params.hostRoot)) ?? params.hostRoot;
  if (currentTarget !== expectedTarget) {
    return {
      packageName,
      packageDir: params.packageDir,
      reason: `${linkPath} points to ${currentTarget} instead of ${expectedTarget}`,
    };
  }
  return null;
}

export async function auditAetherPeerDependencyLink(params: {
  packageDir: string;
  packageName?: string;
}): Promise<AetherPeerLinkAuditIssue | null> {
  const packageName = params.packageName ?? path.basename(params.packageDir);
  const hostRoot = resolveAetherPackageRootSync({
    argv1: process.argv[1],
    moduleUrl: import.meta.url,
    cwd: process.cwd(),
  });
  if (!hostRoot) {
    return {
      packageName,
      packageDir: params.packageDir,
      reason: "could not locate aether package root",
    };
  }
  return await auditAetherPeerDependency({
    hostRoot,
    packageDir: params.packageDir,
    packageName,
  });
}

/** Audit the installed host only when the package actually declares an Aether dependency. */
export async function auditDeclaredAetherHostDependency(params: {
  packageDir: string;
  packageName?: string;
}): Promise<AetherPeerLinkAuditIssue | null> {
  const dependencies = await readPackageAetherLinkDependencies(params.packageDir);
  if (!Object.hasOwn(dependencies, "aether")) {
    return null;
  }
  return await auditAetherPeerDependencyLink(params);
}

async function ensureRealNodeModulesDir(params: {
  installedDir: string;
  logger: PluginPeerLinkLogger;
  beforePersistentEffect?: () => void | Promise<void>;
}): Promise<string | null> {
  const nodeModulesDir = path.join(params.installedDir, "node_modules");
  try {
    const existing = await fs.lstat(nodeModulesDir);
    if (!existing.isDirectory() || existing.isSymbolicLink()) {
      params.logger.warn?.(
        `Skipping aether peerDependency link because ${nodeModulesDir} is not a real directory.`,
      );
      return null;
    }
    return nodeModulesDir;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
      throw error;
    }
  }

  await params.beforePersistentEffect?.();
  await fs.mkdir(nodeModulesDir, { recursive: true });
  const created = await fs.lstat(nodeModulesDir);
  if (!created.isDirectory() || created.isSymbolicLink()) {
    params.logger.warn?.(
      `Skipping aether peerDependency link because ${nodeModulesDir} is not a real directory.`,
    );
    return null;
  }
  return nodeModulesDir;
}

async function linkAetherPeerDependency(params: {
  hostRoot: string;
  installedDir: string;
  peerName: string;
  logger: PluginPeerLinkLogger;
  beforePersistentEffect?: () => void | Promise<void>;
}): Promise<AetherPeerLinkResult> {
  const nodeModulesDir = await ensureRealNodeModulesDir({
    installedDir: params.installedDir,
    logger: params.logger,
    beforePersistentEffect: params.beforePersistentEffect,
  });
  if (!nodeModulesDir) {
    return "skipped";
  }

  const linkPath = path.join(nodeModulesDir, params.peerName);
  const expectedTarget = (await safeRealpath(params.hostRoot)) ?? params.hostRoot;
  const currentTarget = await safeRealpath(linkPath);
  if (currentTarget === expectedTarget) {
    return "unchanged";
  }

  const warnFailure = (err: unknown): AetherPeerLinkResult => {
    params.logger.warn?.(`Failed to symlink peerDependency "${params.peerName}": ${String(err)}`);
    return "skipped";
  };
  let existing;
  try {
    existing = await fs.lstat(linkPath).catch((err: unknown) => {
      if (hasErrnoCode(err, "ENOENT")) {
        return null;
      }
      throw err;
    });
    if (
      existing &&
      !existing.isSymbolicLink() &&
      !(
        params.peerName === "aether" &&
        existing.isDirectory() &&
        (await readPackageName(linkPath)) === "aether"
      )
    ) {
      params.logger.warn?.(
        `Skipping aether peerDependency link because ${linkPath} already exists and is not a symlink.`,
      );
      return "skipped";
    }
  } catch (err) {
    return warnFailure(err);
  }

  // Keep initiating-owner refusal outside filesystem warning conversion. Each
  // awaited mutation also separates the next effect from its authority check.
  if (existing) {
    await params.beforePersistentEffect?.();
    try {
      if (existing.isSymbolicLink()) {
        await fs.unlink(linkPath);
      } else {
        await fs.rm(linkPath, { recursive: true, force: true });
      }
    } catch (err) {
      return warnFailure(err);
    }
  }
  await params.beforePersistentEffect?.();
  try {
    await fs.symlink(params.hostRoot, linkPath, "junction");
    params.logger.info?.(`Linked peerDependency "${params.peerName}" -> ${params.hostRoot}`);
    return "linked";
  } catch (err) {
    return warnFailure(err);
  }
}

async function readPackageName(packageDir: string): Promise<string | undefined> {
  const manifest = await readSafePackageManifest(packageDir);
  return typeof manifest?.name === "string" ? manifest.name : undefined;
}

/**
 * Symlink the host aether package for plugins that declare it as a dependency.
 * Plugin package managers still own third-party dependencies; this only wires
 * the host SDK package into the plugin-local Node graph.
 */
export async function linkAetherPeerDependencies(params: {
  installedDir: string;
  peerDependencies: Record<string, string>;
  logger: PluginPeerLinkLogger;
  /** Explicit source setup uses its selected checkout instead of the running host. */
  hostRoot?: string;
  beforePersistentEffect?: () => void | Promise<void>;
}): Promise<{ repaired: number; skipped: number }> {
  const peers = Object.keys(params.peerDependencies).filter((name) => name === "aether");
  if (peers.length === 0) {
    return { repaired: 0, skipped: 0 };
  }

  const hostRoot =
    params.hostRoot ??
    resolveAetherPackageRootSync({
      argv1: process.argv[1],
      moduleUrl: import.meta.url,
      cwd: process.cwd(),
    });
  if (!hostRoot) {
    params.logger.warn?.(
      "Could not locate aether package root to symlink peerDependencies; plugin may fail to resolve aether at runtime.",
    );
    return { repaired: 0, skipped: peers.length };
  }

  let repaired = 0;
  let skipped = 0;
  for (const peerName of peers) {
    const result = await linkAetherPeerDependency({
      hostRoot,
      installedDir: params.installedDir,
      peerName,
      logger: params.logger,
      beforePersistentEffect: params.beforePersistentEffect,
    });
    if (result === "linked") {
      repaired += 1;
    } else if (result === "skipped") {
      skipped += 1;
    }
  }
  return { repaired, skipped };
}

/**
 * Repair only npm-owned legacy installs named by the authoritative install ledger.
 * Local/path installs and symlink escapes remain developer-owned and are never mutated.
 */
export async function reconcileRegisteredAetherHostLinks(params: {
  installRecords: Record<string, PluginInstallRecord>;
  extensionsDir: string;
  env?: NodeJS.ProcessEnv;
  mode: "audit" | "repair";
  logger?: PluginPeerLinkLogger;
  beforePersistentEffect?: () => void | Promise<void>;
  onPackageReadError?: (error: unknown, packageDir: string) => void;
}): Promise<RegisteredAetherHostLinkResult> {
  const extensionsRoot = path.resolve(params.extensionsDir);
  const extensionsRootRealPath = await safeRealpath(extensionsRoot);
  if (!extensionsRootRealPath) {
    return { checked: 0, repaired: 0, skipped: 0, issues: [] };
  }

  let checked = 0;
  let repaired = 0;
  let skipped = 0;
  const issues: AetherPeerLinkAuditIssue[] = [];
  for (const [pluginId, record] of Object.entries(params.installRecords).toSorted(
    ([left], [right]) => left.localeCompare(right),
  )) {
    if (record.source !== "npm" || !record.installPath?.trim()) {
      continue;
    }

    let packageDir: string;
    let expectedPackageDir: string;
    try {
      packageDir = path.resolve(resolveUserPath(record.installPath, params.env));
      expectedPackageDir = path.resolve(resolvePluginInstallDir(pluginId, extensionsRoot));
    } catch {
      continue;
    }
    if (packageDir !== expectedPackageDir) {
      continue;
    }

    const packageRealPath = await safeRealpath(packageDir);
    const expectedPackageRealPath = path.join(
      extensionsRootRealPath,
      path.relative(extensionsRoot, expectedPackageDir),
    );
    // Ledger paths cannot alias an outside directory or another developer-owned plugin in this root.
    if (
      !packageRealPath ||
      !isPathInside(extensionsRootRealPath, packageRealPath) ||
      packageRealPath !== expectedPackageRealPath
    ) {
      continue;
    }

    let dependencies: Record<string, string>;
    try {
      dependencies = await readPackageAetherLinkDependencies(packageDir);
    } catch (error) {
      if (!params.onPackageReadError) {
        throw error;
      }
      params.onPackageReadError(error, packageDir);
      skipped += 1;
      continue;
    }
    if (!Object.hasOwn(dependencies, "aether")) {
      continue;
    }
    checked += 1;

    const issue = await auditAetherPeerDependencyLink({
      packageDir,
      packageName: pluginId,
    });
    if (!issue) {
      continue;
    }
    issues.push(issue);
    if (params.mode !== "repair") {
      continue;
    }

    const result = await linkAetherPeerDependencies({
      installedDir: packageDir,
      peerDependencies: dependencies,
      logger: params.logger ?? {},
      beforePersistentEffect: params.beforePersistentEffect,
    });
    repaired += result.repaired;
    skipped += result.skipped;
  }
  return { checked, repaired, skipped, issues };
}

export async function relinkAetherPeerDependenciesInManagedNpmRoot(params: {
  npmRoot: string;
  logger: PluginPeerLinkLogger;
  beforePersistentEffect?: () => void | Promise<void>;
  onPackageReadError?: (error: unknown, packageDir: string) => void;
}): Promise<RelinkManagedNpmRootResult> {
  let checked = 0;
  let attempted = 0;
  let repaired = 0;
  let skipped = 0;
  for (const packageDir of await listManagedNpmRootPackageDirs(params.npmRoot)) {
    let aetherLinkDependencies: Record<string, string>;
    try {
      aetherLinkDependencies = await readPackageAetherLinkDependencies(packageDir);
    } catch (error) {
      if (!params.onPackageReadError) {
        throw error;
      }
      params.onPackageReadError(error, packageDir);
      skipped += 1;
      continue;
    }
    if (!Object.hasOwn(aetherLinkDependencies, "aether")) {
      continue;
    }
    checked += 1;
    const result = await linkAetherPeerDependencies({
      installedDir: packageDir,
      peerDependencies: aetherLinkDependencies,
      logger: params.logger,
      beforePersistentEffect: params.beforePersistentEffect,
    });
    attempted += 1;
    repaired += result.repaired;
    skipped += result.skipped;
  }
  return { checked, attempted, repaired, skipped };
}

export async function auditAetherPeerDependenciesInManagedNpmRoot(params: {
  npmRoot: string;
  onPackageReadError?: (error: unknown, packageDir: string) => void;
}): Promise<AuditManagedNpmRootResult> {
  const hostRoot = resolveAetherPackageRootSync({
    argv1: process.argv[1],
    moduleUrl: import.meta.url,
    cwd: process.cwd(),
  });
  if (!hostRoot) {
    return { checked: 0, broken: 0, issues: [] };
  }

  let checked = 0;
  const issues: AetherPeerLinkAuditIssue[] = [];
  for (const packageDir of await listManagedNpmRootPackageDirs(params.npmRoot)) {
    let aetherLinkDependencies: Record<string, string>;
    try {
      aetherLinkDependencies = await readPackageAetherLinkDependencies(packageDir);
    } catch (error) {
      if (!params.onPackageReadError) {
        throw error;
      }
      params.onPackageReadError(error, packageDir);
      continue;
    }
    if (!Object.hasOwn(aetherLinkDependencies, "aether")) {
      continue;
    }
    checked += 1;
    const issue = await auditAetherPeerDependency({
      hostRoot,
      npmRoot: params.npmRoot,
      packageDir,
    });
    if (issue) {
      issues.push(issue);
    }
  }
  return { checked, broken: issues.length, issues };
}
