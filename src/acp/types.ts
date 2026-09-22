/** ACP protocol helpers and Aether agent identity metadata. */
import { VERSION } from "../version.js";
export { normalizeAcpProvenanceMode } from "@aether/acp-core/types";

/** ACP agent identity advertised during protocol initialization. */
export const ACP_AGENT_INFO = {
  name: "aether-acp",
  title: "Aether ACP Gateway",
  version: VERSION,
};
