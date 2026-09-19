import type { AetherPluginApi } from "./plugin-api.types.js";
import type { AetherPluginConfigSchema } from "./plugin-config-schema.types.js";
import type { PluginKind } from "./plugin-kind.types.js";
import type {
  AetherPluginReloadRegistration,
  AetherPluginSecurityAuditCollector,
} from "./plugin-registration.types.js";
import type { AetherPluginNodeHostCommand } from "./types.node-host.js";

/** Module-level plugin definition loaded from a native plugin entry file. */
export type AetherPluginDefinition = {
  id?: string;
  name?: string;
  description?: string;
  version?: string;
  /**
   * @deprecated Declare exclusive plugin kind in `aether.plugin.json` via
   * manifest `kind`. Runtime-exported `kind` is kept as a compatibility
   * fallback for older plugins and may require loading plugin runtime on
   * metadata-only command paths.
   */
  kind?: PluginKind | PluginKind[];
  configSchema?: AetherPluginConfigSchema;
  reload?: AetherPluginReloadRegistration;
  nodeHostCommands?: AetherPluginNodeHostCommand[];
  securityAuditCollectors?: AetherPluginSecurityAuditCollector[];
  register?: (api: AetherPluginApi) => void;
};

export type AetherPluginModule = AetherPluginDefinition | ((api: AetherPluginApi) => void);
