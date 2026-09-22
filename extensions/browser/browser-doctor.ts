/**
 * Browser doctor API barrel. It exposes legacy profile cleanup and Chrome MCP
 * readiness helpers for Aether doctor.
 */
export {
  detectLegacyAetherBrowserProfileResidue,
  maybeArchiveLegacyAetherBrowserProfileResidue,
  maybeRepairOwnedChromeExtensionNativeHosts,
  noteChromeMcpBrowserReadiness,
} from "./src/doctor-browser.js";
export type { LegacyAetherBrowserProfileResidue } from "./src/doctor-browser.js";
