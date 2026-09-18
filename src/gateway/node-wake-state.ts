// Gateway-owned node-work lifecycle and cancellation state.
import {
  nodeWakeOwnerBySignal,
  nodeWakeStateByOwner,
  nodeWakeStateKey,
  type NodeWakeOwnerState,
} from "./node-wake-state-store.js";

export type NodeWakeLifecycle = AbortSignal;

function getOrCreateNodeWakeOwner(nodeId: string, pairingGeneration?: string): NodeWakeOwnerState {
  const normalizedNodeId = nodeId.trim();
  const stateKey = nodeWakeStateKey(normalizedNodeId, pairingGeneration);
  const existing = nodeWakeStateByOwner.get(stateKey);
  if (existing) {
    return existing;
  }
  const created: NodeWakeOwnerState = {
    nodeId: normalizedNodeId,
    stateKey,
  };
  nodeWakeStateByOwner.set(stateKey, created);
  return created;
}

function deleteIdleNodeWakeOwner(owner: NodeWakeOwnerState): void {
  if (owner.lifecycle?.users) {
    return;
  }
  owner.lifecycle?.controller.abort();
  if (owner.lifecycle) {
    nodeWakeOwnerBySignal.delete(owner.lifecycle.controller.signal);
  }
  nodeWakeStateByOwner.delete(owner.stateKey);
}

export function captureNodeWakeLifecycle(
  nodeId: string,
  pairingGeneration?: string,
): NodeWakeLifecycle {
  const owner = getOrCreateNodeWakeOwner(nodeId, pairingGeneration);
  if (!owner.lifecycle || owner.lifecycle.controller.signal.aborted) {
    owner.lifecycle = { controller: new AbortController(), users: 0 };
    nodeWakeOwnerBySignal.set(owner.lifecycle.controller.signal, owner);
  }
  owner.lifecycle.users += 1;
  return owner.lifecycle.controller.signal;
}

export function isNodeWakeLifecycleCurrent(
  nodeId: string,
  lifecycle: NodeWakeLifecycle,
  pairingGeneration?: string,
): boolean {
  const owner = nodeWakeOwnerBySignal.get(lifecycle);
  const expectedStateKey = nodeWakeStateKey(nodeId, pairingGeneration);
  return (
    !lifecycle.aborted &&
    owner?.nodeId === nodeId.trim() &&
    owner.stateKey === expectedStateKey &&
    nodeWakeStateByOwner.get(expectedStateKey) === owner &&
    owner.lifecycle?.controller.signal === lifecycle
  );
}

export function releaseNodeWakeLifecycle(nodeId: string, lifecycle: NodeWakeLifecycle): void {
  const owner = nodeWakeOwnerBySignal.get(lifecycle);
  if (
    owner?.nodeId !== nodeId.trim() ||
    nodeWakeStateByOwner.get(owner.stateKey) !== owner ||
    owner.lifecycle?.controller.signal !== lifecycle
  ) {
    return;
  }
  owner.lifecycle.users = Math.max(0, owner.lifecycle.users - 1);
  deleteIdleNodeWakeOwner(owner);
}

export function clearNodeWakeState(nodeId: string): void {
  const normalizedNodeId = nodeId.trim();
  for (const owner of nodeWakeStateByOwner.values()) {
    if (owner.nodeId !== normalizedNodeId) {
      continue;
    }
    deleteIdleNodeWakeOwner(owner);
  }
}

export function invalidateNodeWakeState(nodeId: string): void {
  const normalizedNodeId = nodeId.trim();
  for (const owner of nodeWakeStateByOwner.values()) {
    if (owner.nodeId !== normalizedNodeId) {
      continue;
    }
    owner.lifecycle?.controller.abort();
    if (owner.lifecycle) {
      nodeWakeOwnerBySignal.delete(owner.lifecycle.controller.signal);
    }
    nodeWakeStateByOwner.delete(owner.stateKey);
  }
}
