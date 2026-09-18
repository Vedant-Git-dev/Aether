// Parses gateway process command lines for process discovery.
import { normalizeLowercaseStringOrEmpty } from "@aether/normalization-core/string-coerce";
import { normalizeStringEntries } from "@aether/normalization-core/string-normalization";

function normalizeProcArg(arg: string): string {
  return normalizeLowercaseStringOrEmpty(arg.replaceAll("\\", "/"));
}

const ENTRY_CANDIDATES = [
  "dist/index.js",
  "dist/entry.js",
  "aether.mjs",
  "scripts/run-app.mjs",
  "src/entry.ts",
  "src/index.ts",
] as const;

export function parseProcCmdline(raw: string): string[] {
  return normalizeStringEntries(raw.split("\0"));
}

export function isAetherArgv(args: string[]): boolean {
  const normalized = args.map(normalizeProcArg);
  const exe = (normalized[0] ?? "").replace(/\.(bat|cmd|exe)$/i, "");
  if (normalized.some((arg) => ENTRY_CANDIDATES.some((entry) => arg.endsWith(entry)))) {
    return true;
  }
  return exe.endsWith("/aether") || exe === "aether";
}

export function isAetherCommandArgv(args: string[], command: string): boolean {
  const normalizedCommand = normalizeProcArg(command);
  return args.some((arg) => normalizeProcArg(arg) === normalizedCommand) && isAetherArgv(args);
}

export function isGatewayArgv(args: string[], opts?: { allowGatewayBinary?: boolean }): boolean {
  const normalized = args.map(normalizeProcArg);
  const exe = (normalized[0] ?? "").replace(/\.(bat|cmd|exe)$/i, "");
  const isGatewayBinary = exe.endsWith("/aether-gateway") || exe === "aether-gateway";
  if (!isAetherCommandArgv(args, "gateway")) {
    return opts?.allowGatewayBinary === true && isGatewayBinary;
  }
  return true;
}
