import type { AetherConfig } from "aether/plugin-sdk/memory-core-host-engine-foundation";

export function isolateMemoryManagerTestConfig(cfg: AetherConfig): AetherConfig {
  return {
    ...cfg,
    plugins: {
      ...cfg.plugins,
      enabled: cfg.plugins?.enabled ?? false,
    },
  };
}
