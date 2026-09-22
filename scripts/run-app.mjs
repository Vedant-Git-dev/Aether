#!/usr/bin/env node
// Minimal app launcher: runs the Aether CLI/gateway straight from TypeScript
// sources via tsx. Unlike scripts/run-node.mjs (the dev runner) this does NOT
// build dist/, run postbuild steps, watch files, or restart on changes — it
// starts exactly one process: node --import tsx src/entry.ts <args>.
//
// Usage:
//   node scripts/run-app.mjs gateway          # start the gateway (default)
//   node scripts/run-app.mjs onboard          # any other CLI command
//   pnpm app -- gateway --port 18789
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const args = process.argv.slice(2);
const cliArgs = args.length > 0 ? args : ["gateway"];

const child = spawn(
  process.execPath,
  ["--import", "tsx", path.join(root, "src", "entry.ts"), ...cliArgs],
  {
    cwd: root,
    stdio: "inherit",
    env: {
      ...process.env,
      // Prefer this checkout's bundled plugins over tracked installs.
      AETHER_DEV_SOURCE_ROOT: process.env.AETHER_DEV_SOURCE_ROOT ?? root,
    },
  },
);

child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code ?? 1);
});
