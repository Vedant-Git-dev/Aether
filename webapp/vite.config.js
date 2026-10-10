// Builds straight into src/aether/web — the directory FastAPI serves at
// /assets. base: "/assets/" keeps the existing mount and GET / working
// unchanged; assetsDir: "" keeps hashed files at /assets/index-*.js instead
// of /assets/assets/index-*.js.
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Dev proxy targets the real instance by default — a locally running `aether`
// on port 8000, so plain `npm run dev` just works. Testing against the e2e
// fixture (python tests/e2e/dev_server.py, port 8731) is explicit opt-in:
// set AETHER_DEV_API and run `npm run dev:test`, which skips without it.
const API_TARGET = process.env.AETHER_DEV_API || "http://127.0.0.1:8000";
const WS_TARGET = API_TARGET.replace(/^http/, "ws");

export default defineConfig({
  base: "/assets/",
  plugins: [react(), tailwindcss()],
  build: {
    outDir: "../src/aether/web",
    emptyOutDir: true,
    assetsDir: "",
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: API_TARGET },
      "/ws": { target: WS_TARGET, ws: true },
    },
  },
});
