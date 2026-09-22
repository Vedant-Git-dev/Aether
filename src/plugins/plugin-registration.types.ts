import type { IncomingMessage, ServerResponse } from "node:http";
import type { Duplex } from "node:stream";
import type { Result } from "@aether/normalization-core/result";
import type { Command } from "commander";
import type { MessageReceipt } from "../channels/message/types.js";
import type { AetherConfig } from "../config/types.aether.js";
import type { ApprovalScope } from "../infra/approval-scope.js";
import type { InternalDiagnosticEventInterest } from "../infra/diagnostic-event-listener-presence.js";
import type {
  DiagnosticEventPrivateData,
  DiagnosticEventInput,
  DiagnosticEventMetadata,
  DiagnosticEventPayload,
} from "../infra/diagnostic-events.js";
import type { DiagnosticTracePropagationBridge as DiagnosticTracePropagationBridgeContract } from "../infra/diagnostic-trace-propagation.js";
import type { SecurityAuditFinding } from "../security/audit.types.js";
import type { DeliveryContext } from "../utils/delivery-context.types.js";
import type { PluginLogger } from "./logger-types.js";
import type { AetherPluginNodeWorkspace } from "./types.node-host.js";

type ChannelPlugin = import("../channels/plugins/types.plugin.js").ChannelPlugin;
type DiagnosticTracePropagationBridge = DiagnosticTracePropagationBridgeContract<
  DiagnosticEventPayload,
  DiagnosticEventMetadata
>;

type PluginInteractiveHandlerResult = {
  handled?: boolean;
} | void;

export type PluginInteractiveRegistration<
  TContext = unknown,
  TChannel extends string = string,
  TResult = PluginInteractiveHandlerResult,
> = {
  channel: TChannel;
  namespace: string;
  handler: (ctx: TContext) => Promise<TResult> | TResult;
};

export type PluginInteractiveHandlerRegistration = PluginInteractiveRegistration;

export type AetherPluginHttpRouteAuth = "gateway" | "plugin";
export type AetherPluginHttpRouteMatch = "exact" | "prefix";
export type AetherPluginGatewayRuntimeScopeSurface = "write-default" | "trusted-operator";

export type AetherPluginHttpRouteHandler = (
  req: IncomingMessage,
  res: ServerResponse,
) => Promise<boolean | void> | boolean | void;

export type AetherPluginHttpRouteUpgradeHandler = (
  req: IncomingMessage,
  socket: Duplex,
  head: Buffer,
) => Promise<boolean | void> | boolean | void;

export type AetherPluginHttpRouteParams = {
  path: string;
  handler: AetherPluginHttpRouteHandler;
  handleUpgrade?: AetherPluginHttpRouteUpgradeHandler;
  auth: AetherPluginHttpRouteAuth;
  match?: AetherPluginHttpRouteMatch;
  gatewayRuntimeScopeSurface?: AetherPluginGatewayRuntimeScopeSurface;
  nodeCapability?: {
    surface: string;
    ttlMs?: number;
  };
  replaceExisting?: boolean;
};

export type AetherPluginHostedMediaResolver = (
  mediaUrl: string,
) => string | null | undefined | Promise<string | null | undefined>;

export type WidgetPresenterContext = Readonly<{
  messageChannel?: string;
  accountId?: string;
  deliveryContext?: Readonly<DeliveryContext>;
  nativeChannelId?: string;
  currentChannelId?: string;
  currentMessagingTarget?: string;
  sessionKey?: string;
}>;

export type WidgetPresenterDocument = Readonly<{
  kind: "html";
  html: string;
  hostedUrl?: string;
}>;

export type WidgetPresentationError =
  | { code: "no_eligible_node"; message: string }
  | { code: "node_error"; message: string; nodeId?: string }
  | { code: "unavailable"; message: string }
  | { code: "presentation_error"; message: string };

export type WidgetPresentationSuccess =
  | { kind: "node"; nodeId: string; nodeName?: string }
  | { kind: "message"; receipt: MessageReceipt };

type WidgetPresenterBase = {
  description: string;
  availability: (
    context: WidgetPresenterContext,
  ) => Promise<Result<{ available: true }, WidgetPresentationError>>;
  present: (params: {
    document: WidgetPresenterDocument;
    title: string;
    context: WidgetPresenterContext;
  }) => Promise<Result<WidgetPresentationSuccess, WidgetPresentationError>>;
};

export type WidgetPresenter = WidgetPresenterBase &
  (
    | {
        target: "node_panel";
        match?: never;
        capabilities?: never;
      }
    | {
        target: "current_channel";
        match: (context: WidgetPresenterContext) => boolean;
        capabilities: Readonly<{
          sourceKinds: readonly string[];
          maxSourceBytes?: number;
        }>;
      }
  );

export type AetherPluginCliContext = {
  /**
   * Command object where this plugin should register its commands.
   *
   * For root CLI registrations this is the root `aether` program. For nested
   * registrations it is the resolved parent command from `parentPath`.
   */
  program: Command;
  parentPath: readonly string[];
  config: AetherConfig;
  workspaceDir?: string;
  logger: PluginLogger;
};

export type AetherPluginCliRegistrar = (ctx: AetherPluginCliContext) => void | Promise<void>;

/**
 * Top-level CLI metadata for plugin-owned commands.
 *
 * Descriptors are the parse-time contract for lazy plugin CLI registration.
 * If you want Aether to keep a plugin command lazy-loaded while still
 * advertising it at the root CLI level, provide descriptors that cover every
 * top-level command root registered by that plugin CLI surface.
 */
type AetherPluginCliCommandDescriptor = {
  name: string;
  description: string;
  hasSubcommands: boolean;
};

/** Root-command metadata that is available before a plugin registrar is activated. */
export type AetherPluginCliRootCommandDescriptor = AetherPluginCliCommandDescriptor & {
  machineOutput?: (params: { argv: readonly string[]; stdoutIsTTY: boolean }) => boolean;
};

type AetherPluginRootCliRegistrationOptions = {
  /** Omit or pass an empty path for root commands. */
  parentPath?: readonly [];
  commands?: readonly string[];
  descriptors?: readonly AetherPluginCliRootCommandDescriptor[];
};

/** Backward-compatible registration shape for dynamic root or nested paths. */
type AetherPluginLegacyCliRegistrationOptions = {
  parentPath?: readonly string[];
  commands?: readonly string[];
  descriptors?: readonly AetherPluginCliCommandDescriptor[];
};

export type AetherPluginCliRegistrationOptions =
  | AetherPluginRootCliRegistrationOptions
  | AetherPluginLegacyCliRegistrationOptions;

export type AetherPluginNodeCliFeatureOptions = {
  /** Explicit node feature command names owned under `aether nodes`. */
  commands?: string[];
  /**
   * Parse-time command descriptors for lazy node feature CLI registration.
   *
   * Descriptors are registered under `aether nodes`, so a descriptor named
   * `"camera"` exposes `aether nodes camera`.
   */
  descriptors?: AetherPluginCliCommandDescriptor[];
};

export type AetherPluginReloadRegistration = {
  restartPrefixes?: string[];
  hotPrefixes?: string[];
  noopPrefixes?: string[];
};

export type {
  AetherPluginNodeHostCommand,
  AetherPluginNodeHostCommandAvailabilityContext,
  AetherPluginNodeHostCommandIo,
} from "./types.node-host.js";

export type AetherPluginNodeInvokeTransportResult =
  | {
      ok: true;
      payload?: unknown;
      payloadJSON?: string | null;
    }
  | {
      ok: false;
      code?: string;
      message: string;
      details?: Record<string, unknown>;
    };

type AetherPluginNodeInvokeApprovalDecision = "allow-once" | "allow-always" | "deny";

type AetherPluginNodeInvokePolicyApprovalRuntime = {
  request: (input: {
    title: string;
    description: string;
    scope?: ApprovalScope;
    severity?: "info" | "warning" | "critical";
    toolName?: string;
    toolCallId?: string;
    agentId?: string;
    sessionKey?: string;
    allowedDecisions?: readonly AetherPluginNodeInvokeApprovalDecision[];
    timeoutMs?: number;
  }) => Promise<{
    id?: string;
    decision?: AetherPluginNodeInvokeApprovalDecision | null;
  }>;
};

export type AetherPluginNodeInvokePolicyContext = {
  nodeId: string;
  command: string;
  params: unknown;
  timeoutMs?: number;
  idempotencyKey?: string;
  config: AetherConfig;
  pluginConfig?: Record<string, unknown>;
  node?: {
    nodeId: string;
    displayName?: string;
    platform?: string;
    deviceFamily?: string;
    commands?: string[];
  };
  client?: {
    connId?: string;
    scopes?: string[];
  } | null;
  risk?: {
    level: "ordinary" | "high";
    /** Stable, content-free family name; never include user or action arguments. */
    family: string;
  };
  approvals?: AetherPluginNodeInvokePolicyApprovalRuntime;
  /** Full covers only the selected harness's declared node commands; undefined requires a human decision. */
  invokeNodeWithSessionFull?: (input: {
    workspace: AetherPluginNodeWorkspace;
    /** Called only after the host authorizes this exact admitted Full launch. */
    createParams: () => unknown;
  }) => Promise<AetherPluginNodeInvokeTransportResult | undefined>;
  invokeNode: (input?: {
    params?: unknown;
    /** Bind an approved launch to its admitted managed workspace, when present. */
    workspace?: AetherPluginNodeWorkspace;
    timeoutMs?: number;
    idempotencyKey?: string;
  }) => Promise<AetherPluginNodeInvokeTransportResult>;
};

export type AetherPluginNodeInvokePolicyResult =
  | {
      ok: true;
      payload?: unknown;
      payloadJSON?: string | null;
    }
  | {
      ok: false;
      message: string;
      code?: string;
      details?: Record<string, unknown>;
      unavailable?: boolean;
    };

export type AetherPluginNodeInvokePolicy = {
  commands: string[];
  /**
   * Platforms where these node-handled commands should be allowlisted by default.
   * Omit for commands that require explicit `gateway.nodes.commands.allow`.
   */
  defaultPlatforms?: Array<"macos" | "windows" | "linux" | "unknown">;
  /**
   * Dangerous policy commands are filtered out of default allowlists unless
   * explicitly allowed by config.
   */
  dangerous?: boolean;
  /**
   * Explicitly permits one approval to cover later launches on the same managed placement.
   * The scope is a stable semantic capability key, never user or action arguments.
   */
  standingApproval?: {
    kind: "placement";
    scope: string;
  };
  /**
   * Foreground-restricted commands should be queued for foreground delivery
   * when a node reports BACKGROUND_UNAVAILABLE.
   */
  foregroundRestrictedOnIos?: boolean;
  /**
   * Classify exact command arguments before the policy handler or node transport runs.
   * Throwing rejects the invocation before dispatch.
   */
  classifyRisk?: (
    ctx: Pick<AetherPluginNodeInvokePolicyContext, "command" | "params">,
  ) => NonNullable<AetherPluginNodeInvokePolicyContext["risk"]>;
  handle: (
    ctx: AetherPluginNodeInvokePolicyContext,
  ) => Promise<AetherPluginNodeInvokePolicyResult> | AetherPluginNodeInvokePolicyResult;
};

export type AetherPluginSecurityAuditContext = {
  config: AetherConfig;
  sourceConfig: AetherConfig;
  env: NodeJS.ProcessEnv;
  stateDir: string;
  configPath: string;
};

export type AetherPluginSecurityAuditCollector = (
  ctx: AetherPluginSecurityAuditContext,
) => SecurityAuditFinding[] | Promise<SecurityAuditFinding[]>;

export type AetherGatewayDiscoveryAdvertiseContext = {
  machineDisplayName: string;
  gatewayPort: number;
  gatewayTlsEnabled: boolean;
  gatewayTlsFingerprintSha256?: string;
  gatewayDirectReachable: boolean;
  tailnetDns?: string;
  sshPort?: number;
  cliPath?: string;
  minimal: boolean;
};

export type AetherGatewayDiscoveryService = {
  id: string;
  advertise: (
    ctx: AetherGatewayDiscoveryAdvertiseContext,
  ) => void | Promise<void | { stop?: () => void | Promise<void> }>;
};

/** Context passed to long-lived plugin services. */
export type AetherPluginServiceHealth = {
  reportFailure: (error: unknown) => void;
  clearFailure: () => void;
};

export type AetherPluginServiceContext = {
  config: AetherConfig;
  workspaceDir?: string;
  stateDir: string;
  logger: PluginLogger;
  serviceHealth?: AetherPluginServiceHealth;
  /** Gateway-owned scheduler access, revoked when this service stops. */
  getCron?: () => import("./hook-types.js").PluginHookGatewayCronService | undefined;
  gatewayEvents?: import("./gateway-events.js").AetherPluginGatewayEvents;
  startupTrace?: {
    detail?: (name: string, metrics: ReadonlyArray<readonly [string, number | string]>) => void;
    measure: <T>(name: string, run: () => T | Promise<T>) => Promise<T>;
  };
  internalDiagnostics?: {
    /** Identity of the hosting process, available only while this service is active. */
    getRuntimeIdentity?: () => { processInstanceId: string; buildId?: string };
    emit: (event: DiagnosticEventInput, privateData?: DiagnosticEventPrivateData) => void;
    onEvent: (
      listener: (
        event: DiagnosticEventPayload,
        metadata: DiagnosticEventMetadata,
        privateData: DiagnosticEventPrivateData,
      ) => void,
      filter?: InternalDiagnosticEventInterest<DiagnosticEventPayload["type"]>,
      /** Defaults to true; false skips private payload copies and passes a frozen empty object. */
      options?: { includePrivateData?: boolean },
    ) => () => void;
    registerTracePropagationBridge?: (bridge: DiagnosticTracePropagationBridge) => () => void;
  };
};

/** Background service registered by a plugin during `register(api)`. */
export type AetherPluginService = {
  id: string;
  /** Restart this service with committed config when one of these paths changes. */
  reload?: { configPrefixes: readonly string[] };
  start: (ctx: AetherPluginServiceContext) => void | Promise<void>;
  stop?: (ctx: AetherPluginServiceContext) => void | Promise<void>;
};

export type AetherPluginChannelRegistration = {
  plugin: ChannelPlugin;
};

/**
 * Public label exposed to plugin `register(api)` calls.
 *
 * Keep this as a compatibility signal for plugin authors. Loader internals
 * should derive explicit capability booleans from the mode instead of branching
 * on raw strings throughout the code path.
 *
 * - `full`: live runtime activation; long-lived side effects may start.
 * - `discovery`: read-only capability discovery; skip sockets/workers/clients.
 * - `tool-discovery`: capability discovery for executable tools; skip channel runtime hydration.
 * - `setup-only`: lightweight channel setup entry only.
 * - `setup-runtime`: setup flow that also needs the runtime channel entry.
 * - `cli-metadata`: CLI command metadata collection.
 */
export type PluginRegistrationMode =
  | "full"
  | "discovery"
  | "tool-discovery"
  | "setup-only"
  | "setup-runtime"
  | "cli-metadata";
