import { resolveGlobalSingleton } from "aether/plugin-sdk/global-singleton";
import { KeyedAsyncQueue } from "aether/plugin-sdk/keyed-async-queue";

// Dist and source copies share physical clients, so their lifecycle queues must
// share ownership too. Settled entries drain naturally; never clear active tails.
const nativeThreadOwners = resolveGlobalSingleton(
  Symbol.for("aether.codexNativeThreadOwners"),
  () => new KeyedAsyncQueue(),
);

/** Serialize Aether-owned lifecycle changes, not native-internal thread controllers. */
export async function withCodexAppServerThreadMutation<T>(
  threadId: string,
  run: () => Promise<T>,
): Promise<T> {
  return await nativeThreadOwners.enqueue(`thread:${threadId}`, run);
}

/** Serializes bound turns and retirement so detach cannot unsubscribe an active turn. */
export async function withCodexConversationThreadActivity<T>(
  bindingId: string,
  run: () => Promise<T>,
): Promise<T> {
  return await nativeThreadOwners.enqueue(`conversation:${bindingId}`, run);
}
