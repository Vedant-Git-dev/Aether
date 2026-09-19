/** Detects whether a daemon was launched by Aether's container-aware service wrapper. */
import { normalizeOptionalString } from "@aether/normalization-core/string-coerce";

/** Resolves the daemon container hint exposed by managed service environments. */
export function resolveDaemonContainerContext(
  env: Record<string, string | undefined> = process.env,
): string | null {
  return (
    normalizeOptionalString(env.AETHER_CONTAINER_HINT) ||
    normalizeOptionalString(env.AETHER_CONTAINER) ||
    null
  );
}
