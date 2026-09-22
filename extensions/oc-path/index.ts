// OC Path plugin entrypoint registers its Aether integration.
import { definePluginEntry } from "aether/plugin-sdk/plugin-entry";
import { registerOcPathCli } from "./cli-registration.js";

export default definePluginEntry({
  id: "oc-path",
  name: "OC Path",
  description: "Adds the aether path CLI for oc:// workspace file addressing.",
  register(api) {
    registerOcPathCli(api);
  },
});
