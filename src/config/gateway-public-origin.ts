import type { AetherConfig } from "./types.js";

export function resolveGatewayPublicOrigin(
  config: Pick<AetherConfig, "gateway"> | null | undefined,
): string | undefined {
  const raw = config?.gateway?.publicOrigin?.trim();
  if (!raw) {
    return undefined;
  }
  try {
    const parsed = new URL(raw);
    return parsed.pathname === "/" && !parsed.search && !parsed.hash ? parsed.origin : undefined;
  } catch {
    return undefined;
  }
}
