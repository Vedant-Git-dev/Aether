// Application-owned browser push subscription lifecycle.
import { formatUiError } from "../lib/format-error.ts";
import type { ConnectionBootstrapCoordinator } from "./connection-bootstrap.ts";
import type { ApplicationGateway } from "./gateway.ts";
import type {
  WebPushCapabilityAction,
  WebPushCapabilityPatch,
  WebPushCapabilityRuntime,
  WebPushPreferencesResult,
  WebPushSubscriptionState,
} from "./web-push.runtime.ts";

export type WebPushSnapshot = {
  supported: boolean;
  permission: NotificationPermission | "unsupported";
  subscription: WebPushSubscriptionState;
  loading: boolean;
  error?: string | null;
  preferences?: WebPushPreferencesResult | null;
};

export type WebPushCapability = {
  readonly snapshot: WebPushSnapshot;
  subscribe: (listener: () => void) => () => void;
  run: (action: WebPushCapabilityAction) => Promise<void>;
  dispose: () => void;
};

export function createWebPushCapability(
  gateway: ApplicationGateway,
  options: { connectionBootstrap?: ConnectionBootstrapCoordinator } = {},
): WebPushCapability {
  const nav = globalThis.navigator;
  const supported =
    "serviceWorker" in nav &&
    "PushManager" in globalThis &&
    "Notification" in globalThis;
  const snapshot: WebPushSnapshot = {
    supported,
    permission: supported ? Notification.permission : "unsupported",
    subscription: "unknown",
    loading: false,
  };
  const listeners = new Set<() => void>();

  const publish = (patch: WebPushCapabilityPatch) => {
    Object.assign(snapshot, patch);
    for (const listener of listeners) {
      listener();
    }
  };
  const runtime: Promise<WebPushCapabilityRuntime | null> | null = snapshot.supported
    ? import("./web-push.runtime.ts")
        .then(({ createWebPushCapabilityRuntime }) =>
          createWebPushCapabilityRuntime({
            gateway,
            publish,
            connectionBootstrap: options.connectionBootstrap,
          }),
        )
        .catch((error: unknown) => {
          publish({
            supported: false,
            permission: "unsupported",
            subscription: "unknown",
            preferences: null,
            error: formatUiError(error),
          });
          return null;
        })
    : null;
  return {
    snapshot,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    run: (action) => (runtime ? runtime.then((owner) => owner?.run(action)) : Promise.resolve()),
    dispose() {
      void runtime?.then((owner) => owner?.dispose());
      listeners.clear();
    },
  };
}
