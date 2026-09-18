export type NodeWakeOwnerState = {
  nodeId: string;
  stateKey: string;
  lifecycle?: {
    controller: AbortController;
    users: number;
  };
};

export const nodeWakeStateByOwner = new Map<string, NodeWakeOwnerState>();
export const nodeWakeOwnerBySignal = new WeakMap<AbortSignal, NodeWakeOwnerState>();

export function nodeWakeStateKey(nodeId: string, pairingGeneration?: string): string {
  return JSON.stringify([nodeId.trim(), pairingGeneration?.trim() || null]);
}
