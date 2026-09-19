// Node-host plugin command contracts, including the opt-in duplex transport.
import type { AetherConfig } from "../config/types.aether.js";

export type AetherPluginNodeHostCommandAvailabilityContext = {
  /** Node-local configuration used to build this host's Gateway declaration. */
  config: AetherConfig;
  /** Node-host process environment. */
  env: NodeJS.ProcessEnv;
};

export type AetherPluginNodeHostCommandIo = {
  emitChunk(chunk: string): Promise<void>;
  onInput(callback: (payloadJSON: string) => void): void;
  /** Complete binary messages; available when the node host dispatches a duplex command. */
  frames?: {
    send(message: Uint8Array): Promise<void>;
    onMessage(listener: (message: Uint8Array) => void | Promise<void>): () => void;
  };
  signal: AbortSignal;
};

export type AetherPluginNodeWorkspace = {
  workspaceDir: string;
  environmentId: string;
  sessionId: string;
  ownerEpoch: number;
  sessionKey: string;
};

export type AetherPluginNodeHostCommandContext = {
  /** Emit one node-owned event through the active Gateway connection. */
  sendNodeEvent(event: string, payload: unknown): Promise<unknown>;
  /** Agent session that owns this invocation, when the caller supplied one. */
  sessionKey?: string;
  /** Aborts when the Gateway cancels this specific node-host invocation. */
  signal?: AbortSignal;
  /** Prepare local exec policy; call the returned guard synchronously immediately before spawn. */
  prepareExecAuthorization?: (source: "human-approved" | "session-full") => () => void;
  /** Protect one exact node-owned placement workspace for this invocation's lifetime. */
  acquireManagedWorkspace?: (request: AetherPluginNodeWorkspace) => {
    workspaceDir: string;
    /** Stable HOME owned and validated by this exact prepared workspace binding. */
    homeDir?: string;
    release: () => void;
  };
};

type AetherPluginNodeHostCommandBase = {
  command: string;
  cap?: string;
  dangerous?: boolean;
  /** Settle node-local startup before the initial capability declaration; registration stays synchronous. */
  prepare?: (context: AetherPluginNodeHostCommandAvailabilityContext) => Promise<void> | void;
  /** Return false to omit this command and capability from the node declaration. */
  isAvailable?: (context: AetherPluginNodeHostCommandAvailabilityContext) => boolean;
  /** Watch node-local availability and request a fresh Gateway declaration. */
  watchAvailability?: (
    context: AetherPluginNodeHostCommandAvailabilityContext,
    onChange: () => void,
  ) => (() => void) | void;
  /** Release command-owned state when the active Gateway connection closes. */
  onDisconnect?: () => Promise<void> | void;
  /** Optional Computer Use declaration published with this command's node manifest. */
  computerUse?: (context: AetherPluginNodeHostCommandAvailabilityContext) => unknown;
  agentTool?: {
    name: string;
    description: string;
    parameters?: Record<string, unknown>;
    /** Platforms where this tool is allowlisted by default; omit for explicit config only. */
    defaultPlatforms?: Array<"macos" | "windows" | "linux" | "unknown">;
    mcp?: { server: string; tool: string };
  };
};

export type AetherPluginNodeHostCommand = AetherPluginNodeHostCommandBase & {
  // Not a discriminated handle signature: a union of different arities makes
  // plain `command.handle(params)` uncallable for consumers holding the union.
  // The node host enforces io presence for duplex commands at runtime.
  duplex?: boolean;
  handle: (
    paramsJSON?: string | null,
    io?: AetherPluginNodeHostCommandIo,
    context?: AetherPluginNodeHostCommandContext,
  ) => Promise<string>;
};
