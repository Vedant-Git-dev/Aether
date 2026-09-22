// Reads provider ids selected by auth, model, channel, and media configuration.
import { collectConfiguredModelRefs } from "@aether/model-catalog-core/configured-model-refs";
import { asNullableRecord } from "@aether/normalization-core/record-coerce";
import { normalizeNullableString as normalizeId } from "@aether/normalization-core/string-coerce";
import type { AetherConfig } from "../../../config/types.aether.js";

function collectConfiguredProviderIds(cfg: AetherConfig): Set<string> {
  const ids = new Set<string>();
  const add = (value: unknown) => {
    const id = normalizeId(value);
    if (id) {
      ids.add(id.toLowerCase());
    }
  };
  for (const profile of Object.values(asNullableRecord(cfg.auth?.profiles) ?? {})) {
    add(asNullableRecord(profile)?.provider);
  }
  for (const providerId of Object.keys(asNullableRecord(cfg.models?.providers) ?? {})) {
    add(providerId);
  }
  const modelByChannel = asNullableRecord(cfg.channels?.modelByChannel);
  for (const [providerId, channelMap] of Object.entries(modelByChannel ?? {})) {
    add(providerId);
    for (const modelRef of Object.values(asNullableRecord(channelMap) ?? {})) {
      if (typeof modelRef !== "string") {
        continue;
      }
      const slash = modelRef.indexOf("/");
      if (slash > 0) {
        add(modelRef.slice(0, slash));
      }
    }
  }
  for (const { value } of collectConfiguredModelRefs(cfg, {
    includeChannelModelOverrides: false,
  })) {
    const slash = value.indexOf("/");
    if (slash > 0) {
      add(value.slice(0, slash));
    }
  }
  return ids;
}

function collectConfiguredMediaProviderIds(cfg: AetherConfig): Set<string> {
  const ids = new Set<string>();
  const add = (value: unknown) => {
    const id = normalizeId(value);
    if (id) {
      ids.add(id.toLowerCase());
    }
  };
  const addModels = (value: unknown) => {
    if (!Array.isArray(value)) {
      return;
    }
    for (const model of value) {
      add(asNullableRecord(model)?.provider);
    }
  };
  const media = cfg.tools?.media;
  addModels(media?.models);
  return ids;
}

/** Provider ids used by static and installed-registry plugin matching. */
export function collectConfiguredProviderSelectionIds(cfg: AetherConfig): ReadonlySet<string> {
  return new Set([...collectConfiguredProviderIds(cfg), ...collectConfiguredMediaProviderIds(cfg)]);
}

export function collectConfiguredMediaProviderSelectionIds(
  cfg: AetherConfig,
): ReadonlySet<string> {
  return collectConfiguredMediaProviderIds(cfg);
}

export function collectConfiguredModelProviderSelectionIds(
  cfg: AetherConfig,
): ReadonlySet<string> {
  return collectConfiguredProviderIds(cfg);
}
