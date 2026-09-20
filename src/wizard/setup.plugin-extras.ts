// Optional bundled plugin extras: user explicitly picks extra tools to enable.
// Plugin enablement is opt-in, so this step surfaces bundled tools as visible
// choices instead of silent defaults. Channels, providers, and memory are
// handled by their own steps and stay out of this list.
import type { AetherConfig } from "../config/types.aether.js";
import { normalizePluginId, normalizePluginsConfig } from "../plugins/config-state.js";
import { enableExplicitlySelectedPluginInConfig } from "../plugins/enable.js";
import { createLazyRuntimeModule } from "../shared/lazy-runtime.js";
import { t } from "./i18n/index.js";
import type { WizardPrompter } from "./prompts.js";

const loadPluginMetadataSnapshotModule = createLazyRuntimeModule(
  () => import("../plugins/plugin-metadata-snapshot.js"),
);

const SKIP_VALUE = "__skip__";

/** Manifest fields the extras discovery needs; keeps the filter unit-testable. */
type PluginExtraCandidateSource = {
  id: string;
  name?: string;
  description?: string;
  origin: string;
  kind?: unknown;
  channels: readonly string[];
  providers: readonly string[];
  enabledByDefault?: boolean;
};

export type PluginExtraCandidate = {
  id: string;
  name: string;
  description?: string;
};

function hasKind(kind: unknown, target: string): boolean {
  return Array.isArray(kind) ? kind.includes(target) : kind === target;
}

/**
 * Bundled non-channel, non-provider plugins that are not enabled yet and not
 * blocked by the denylist/allowlist policy. Sorted by display name.
 */
export function resolvePluginExtraCandidates(params: {
  manifestPlugins: ReadonlyArray<PluginExtraCandidateSource>;
  config: AetherConfig;
}): PluginExtraCandidate[] {
  const plugins = normalizePluginsConfig(params.config.plugins);
  const candidates: PluginExtraCandidate[] = [];
  for (const plugin of params.manifestPlugins) {
    if (plugin.origin !== "bundled") {
      continue;
    }
    // Channels and providers have dedicated setup steps and auto-enable when
    // configured; memory/context-engine plugins are slot-driven.
    if (plugin.channels.length > 0 || plugin.providers.length > 0) {
      continue;
    }
    if (hasKind(plugin.kind, "memory") || hasKind(plugin.kind, "context-engine")) {
      continue;
    }
    if (plugin.enabledByDefault) {
      continue;
    }
    const id = normalizePluginId(plugin.id);
    if (params.config.plugins?.entries?.[id]?.enabled === true) {
      continue;
    }
    if (plugins.deny.includes(id)) {
      continue;
    }
    if (plugins.allow.length > 0 && !plugins.allow.includes(id)) {
      continue;
    }
    candidates.push({
      id,
      name: plugin.name?.trim() || plugin.id,
      description: plugin.description?.trim() || undefined,
    });
  }
  return candidates.toSorted((left, right) => left.name.localeCompare(right.name));
}

/**
 * Run the optional-plugin extras step for both wizard flows. Selecting nothing
 * (or the skip option) leaves the minimal default: memory plus whatever the
 * model-auth and channel steps already enabled.
 */
export async function setupPluginExtras(params: {
  config: AetherConfig;
  prompter: WizardPrompter;
  workspaceDir?: string;
}): Promise<AetherConfig> {
  const { loadPluginMetadataSnapshot } = await loadPluginMetadataSnapshotModule();
  const snapshot = loadPluginMetadataSnapshot({
    config: params.config,
    workspaceDir: params.workspaceDir,
    env: process.env,
  });
  const candidates = resolvePluginExtraCandidates({
    manifestPlugins: snapshot.plugins,
    config: params.config,
  });
  if (candidates.length === 0) {
    return params.config;
  }

  const selected = await params.prompter.multiselect({
    message: t("wizard.plugins.extrasSelect"),
    options: [
      {
        value: SKIP_VALUE,
        label: t("wizard.plugins.extrasSkip"),
        hint: t("wizard.plugins.extrasSkipHint"),
      },
      ...candidates.map((candidate) => ({
        value: candidate.id,
        label: candidate.name,
        hint: candidate.description,
      })),
    ],
    searchable: true,
  });

  let config = params.config;
  for (const pluginId of selected) {
    if (pluginId === SKIP_VALUE) {
      continue;
    }
    const result = enableExplicitlySelectedPluginInConfig(config, pluginId);
    config = result.config;
    if (!result.enabled) {
      await params.prompter.note(
        t("wizard.plugins.enableFailed", {
          plugin: pluginId,
          reason: result.reason ?? "unknown",
        }),
      );
    }
  }
  return config;
}
