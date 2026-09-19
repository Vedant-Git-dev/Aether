// Product/package naming constants that bridge current Aether manifests with
// legacy Aether keys still seen in older configs and packages.
const PROJECT_NAME = "aether" as const;

const LEGACY_PROJECT_NAMES = ["aether"] as const;

export const MANIFEST_KEY = PROJECT_NAME;

/** Manifest keys accepted only for legacy compatibility. */
export const LEGACY_MANIFEST_KEYS = LEGACY_PROJECT_NAMES;
