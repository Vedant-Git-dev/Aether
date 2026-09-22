import crypto from "node:crypto";
import { stableStringify } from "@aether/normalization-core";
import { redactConfigObject } from "../../config/redact-snapshot.js";
import type { AetherConfig } from "../../config/types.aether.js";

let configFingerprints = new WeakMap<AetherConfig, string>();

export function fingerprintSkillSnapshotConfig(config: AetherConfig): string {
  const cached = configFingerprints.get(config);
  if (cached) {
    return cached;
  }
  const fingerprint = crypto
    .createHash("sha256")
    .update(stableStringify(redactConfigObject(config)))
    .digest("hex");
  configFingerprints.set(config, fingerprint);
  return fingerprint;
}

export function resetSkillSnapshotConfigFingerprintCache(): void {
  configFingerprints = new WeakMap();
}
