// Hook frontmatter helpers parse metadata blocks from hook files.
import { readStringValue } from "@aether/normalization-core/string-coerce";
import { parseFrontmatterBlock } from "../../packages/markdown-core/src/frontmatter.js";
import {
  applyAetherManifestInstallCommonFields,
  getFrontmatterString,
  normalizeStringList,
  parseAetherManifestInstallBase,
  parseFrontmatterBool,
  resolveAetherManifestBlock,
  resolveAetherManifestInstall,
  resolveAetherManifestOs,
  resolveAetherManifestRequires,
} from "../shared/frontmatter.js";
import type {
  AetherHookMetadata,
  HookEntry,
  HookInstallSpec,
  HookInvocationPolicy,
  ParsedHookFrontmatter,
} from "./types.js";

/** Parse HOOK.md frontmatter into the generic hook frontmatter record. */
export function parseHookFrontmatter(content: string): ParsedHookFrontmatter {
  return parseFrontmatterBlock(content);
}

function parseInstallSpec(input: unknown): HookInstallSpec | undefined {
  const parsed = parseAetherManifestInstallBase(input, ["bundled", "npm", "git"]);
  if (!parsed) {
    return undefined;
  }
  const { raw } = parsed;
  const spec = applyAetherManifestInstallCommonFields<HookInstallSpec>(
    {
      kind: parsed.kind as HookInstallSpec["kind"],
    },
    parsed,
  );
  if (typeof raw.package === "string") {
    spec.package = raw.package;
  }
  if (typeof raw.repository === "string") {
    spec.repository = raw.repository;
  }

  return spec;
}

/** Resolve Aether hook metadata from the manifest block in HOOK.md frontmatter. */
export function resolveHookManifestMetadata(
  frontmatter: ParsedHookFrontmatter,
): AetherHookMetadata | undefined {
  const metadataObj = resolveAetherManifestBlock({ frontmatter });
  if (!metadataObj) {
    return undefined;
  }
  const requires = resolveAetherManifestRequires(metadataObj);
  const install = resolveAetherManifestInstall(metadataObj, parseInstallSpec);
  const osRaw = resolveAetherManifestOs(metadataObj);
  const eventsRaw = normalizeStringList(metadataObj.events);
  return {
    always: typeof metadataObj.always === "boolean" ? metadataObj.always : undefined,
    emoji: readStringValue(metadataObj.emoji),
    homepage: readStringValue(metadataObj.homepage),
    hookKey: readStringValue(metadataObj.hookKey),
    export: readStringValue(metadataObj.export),
    os: osRaw.length > 0 ? osRaw : undefined,
    events: eventsRaw.length > 0 ? eventsRaw : [],
    requires,
    install: install.length > 0 ? install : undefined,
  };
}

/** Resolve invocation policy from top-level hook frontmatter flags. */
export function resolveHookInvocationPolicy(
  frontmatter: ParsedHookFrontmatter,
): HookInvocationPolicy {
  return {
    enabled: parseFrontmatterBool(getFrontmatterString(frontmatter, "enabled"), true),
  };
}

/** Resolve the config key for a hook, honoring metadata hookKey overrides. */
export function resolveHookKey(hookName: string, entry?: Pick<HookEntry, "metadata">): string {
  return entry?.metadata?.hookKey ?? hookName;
}
