import type { ProgressCard, ProgressCardStep } from "../../packages/gateway-protocol/src/index.js";
import {
  readSessionProgressCard,
  writeSessionProgressCard,
} from "../session-cards/progress-card-store.js";
import { withAetherAgentDatabaseReadOnly } from "../state/aether-agent-db-readonly.js";
import { runAetherAgentWriteTransaction } from "../state/aether-agent-db.js";
import { resolveGatewaySessionDatabase } from "./board-store.js";

export type ProgressCardStore = {
  get(sessionKey: string, agentId?: string): Promise<ProgressCard | null>;
  put(
    sessionKey: string,
    input: {
      markdown?: string;
      steps?: ProgressCardStep[];
      expectedRevision?: number;
      // The storage owner checks authority inside its write transaction.
      assertCurrent?: () => void;
    },
    agentId?: string,
  ): Promise<{ card: ProgressCard | null }>;
};

export const progressCardStore: ProgressCardStore = {
  async get(sessionKey, agentId) {
    const resolved = resolveGatewaySessionDatabase(sessionKey, agentId);
    const result = withAetherAgentDatabaseReadOnly(
      (database) => readSessionProgressCard(database.db, resolved.sessionKey),
      resolved,
    );
    return result.found ? result.value : null;
  },
  async put(sessionKey, input, agentId) {
    const resolved = resolveGatewaySessionDatabase(sessionKey, agentId);
    const result = runAetherAgentWriteTransaction(
      (transactionDatabase) => {
        input.assertCurrent?.();
        return writeSessionProgressCard(transactionDatabase.db, resolved.sessionKey, input);
      },
      resolved,
      { operationLabel: "progress-card.put" },
    );
    return "card" in result ? result : { card: null };
  },
};
