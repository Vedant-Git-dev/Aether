// Reef plugin module registers headless CLI commands so agents can drive
// registration, pairing, and status without the interactive wizard.
import type { AetherPluginApi } from "aether/plugin-sdk/channel-plugin-common";

export function registerReefCliMetadata(api: AetherPluginApi) {
  api.registerCli(
    async ({ program }) => {
      const { registerReefCli } = await import("./cli.js");
      registerReefCli({ program });
    },
    {
      descriptors: [
        {
          name: "reef",
          description: "Register on a Reef relay and manage guarded claw-to-claw friendships",
          hasSubcommands: true,
        },
      ],
    },
  );
}
