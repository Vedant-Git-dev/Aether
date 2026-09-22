/** Facade-backed doctor checks and cleanup for bundled browser plugin state. */
import fs from "node:fs";
import path from "node:path";
import { note } from "../../packages/terminal-core/src/note.js";
import type { AetherConfig } from "../config/types.aether.js";
import { loadBundledPluginPublicSurfaceModuleSyncCore } from "../plugin-sdk/facade-loader.js";
import { resolveConfigDir } from "../utils.js";

type BrowserDoctorDeps = {
  platform?: NodeJS.Platform;
  noteFn?: typeof note;
  env?: NodeJS.ProcessEnv;
  getUid?: () => number;
  resolveManagedExecutable?: (
    resolved: unknown,
    platform: NodeJS.Platform,
  ) => { path: string } | null;
  resolveChromeExecutable?: (platform: NodeJS.Platform) => { path: string } | null;
  readVersion?: (executablePath: string) => string | null;
  configDir?: string;
  pathExists?: (targetPath: string) => boolean;
};

type BrowserDoctorRepairDeps = {
  env?: NodeJS.ProcessEnv;
  configDir?: string;
  pathExists?: (targetPath: string) => boolean;
  movePathToTrash?: (targetPath: string) => Promise<string>;
};

/** Legacy browser profile paths detected before cleanup moves them aside. */
export type LegacyAetherBrowserProfileResidue = {
  legacyProfileDir: string;
  legacyUserDataDir: string;
  canonicalUserDataDir: string;
};

type BrowserNativeHostRepairResult = {
  status?: "repaired" | "skipped" | "failed";
  reason?: string;
  changes: string[];
  warnings: string[];
};

type BrowserDoctorSurface = {
  noteChromeMcpBrowserReadiness: (cfg: AetherConfig, deps?: BrowserDoctorDeps) => Promise<void>;
  detectLegacyAetherBrowserProfileResidue?: (
    cfg: AetherConfig,
    deps?: BrowserDoctorRepairDeps,
  ) => LegacyAetherBrowserProfileResidue | null;
  maybeArchiveLegacyAetherBrowserProfileResidue?: (
    cfg: AetherConfig,
    deps?: BrowserDoctorRepairDeps,
  ) => Promise<{ changes: string[]; warnings: string[] }>;
  maybeRepairOwnedChromeExtensionNativeHosts?: () => Promise<BrowserNativeHostRepairResult>;
};

function loadBrowserDoctorSurface(): BrowserDoctorSurface {
  return loadBundledPluginPublicSurfaceModuleSyncCore<BrowserDoctorSurface>({
    dirName: "browser",
    artifactBasename: "browser-doctor.js",
  });
}

/** Reports the browser plugin's native-host repair outcome, including intentional skips. */
export async function maybeRepairOwnedChromeExtensionNativeHosts(): Promise<BrowserNativeHostRepairResult> {
  try {
    const repair = loadBrowserDoctorSurface().maybeRepairOwnedChromeExtensionNativeHosts;
    return repair ? await repair() : { changes: [], warnings: [] };
  } catch (error) {
    return {
      changes: [],
      warnings: [
        `Browser extension native-host repair is unavailable: ${error instanceof Error ? error.message : String(error)}`,
      ],
    };
  }
}

function mayHaveLegacyAetherBrowserProfileResidue(deps?: BrowserDoctorRepairDeps): boolean {
  const configDir = deps?.configDir ?? resolveConfigDir(deps?.env ?? process.env);
  const legacyProfileDir = path.join(configDir, "browser", "aether");
  const legacyUserDataDir = path.join(legacyProfileDir, "user-data");
  const pathExists = deps?.pathExists ?? fs.existsSync;
  try {
    return pathExists(legacyProfileDir) || pathExists(legacyUserDataDir);
  } catch {
    return true;
  }
}

/** Emits browser readiness notes through the bundled browser plugin doctor surface. */
export async function noteChromeMcpBrowserReadiness(cfg: AetherConfig, deps?: BrowserDoctorDeps) {
  try {
    await loadBrowserDoctorSurface().noteChromeMcpBrowserReadiness(cfg, deps);
  } catch (error) {
    const noteFn = deps?.noteFn ?? note;
    const message = error instanceof Error ? error.message : String(error);
    noteFn(`- Browser health check is unavailable: ${message}`, "Browser");
  }
}

/** Detects old aether browser profile residue without loading plugin cleanup when paths are absent. */
export async function detectLegacyAetherBrowserProfileResidue(
  cfg: AetherConfig,
  deps?: BrowserDoctorRepairDeps,
): Promise<LegacyAetherBrowserProfileResidue | null> {
  if (!mayHaveLegacyAetherBrowserProfileResidue(deps)) {
    return null;
  }
  const detect = loadBrowserDoctorSurface().detectLegacyAetherBrowserProfileResidue;
  if (!detect) {
    return null;
  }
  return detect(cfg, deps);
}

/** Archives legacy aether browser profile residue through the browser plugin repair hook. */
export async function maybeArchiveLegacyAetherBrowserProfileResidue(
  cfg: AetherConfig,
  deps?: BrowserDoctorRepairDeps,
): Promise<{ changes: string[]; warnings: string[] }> {
  if (!mayHaveLegacyAetherBrowserProfileResidue(deps)) {
    return { changes: [], warnings: [] };
  }
  try {
    const repair = loadBrowserDoctorSurface().maybeArchiveLegacyAetherBrowserProfileResidue;
    if (!repair) {
      return { changes: [], warnings: [] };
    }
    return await repair(cfg, deps);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return {
      changes: [],
      warnings: [`Browser profile cleanup is unavailable: ${message}`],
    };
  }
}
