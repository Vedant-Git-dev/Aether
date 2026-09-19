import type { AetherConfig } from "../config/types.aether.js";

const DEFAULT_REMOTE_MODEL_CATALOG_URL = "https://catalog.aether.ai/models/v1/catalog.json";

export function isRemoteModelCatalogRefreshEnabled(config: AetherConfig): boolean {
  return config.models?.catalogRefresh?.enabled !== false;
}

export function resolveRemoteCatalogUrl(config: AetherConfig): string {
  return config.models?.catalogRefresh?.url?.trim() || DEFAULT_REMOTE_MODEL_CATALOG_URL;
}
