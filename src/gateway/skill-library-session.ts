import type { AetherConfig } from "../config/types.aether.js";
import { seedSkillLibrarySelection } from "../skills/library/selection.js";
import type { TrustedSessionCreation } from "./server-methods/session-creation-provenance.js";
import type { GatewayClient } from "./server-methods/shared-types.js";

/** Selection is prepared from this request's real principal, never reconstructed from provenance. */
export function prepareSkillLibrarySessionCreation(
  client: GatewayClient | null | undefined,
  cfg: AetherConfig | (() => AetherConfig),
  creation: TrustedSessionCreation,
): TrustedSessionCreation {
  if (
    !client?.authenticatedUserProfile ||
    client.internal?.syntheticClient ||
    creation.via === "spawn"
  ) {
    return creation;
  }
  return {
    ...creation,
    skillLibrarySelections: seedSkillLibrarySelection({
      profileId: client.authenticatedUserProfile.profileId,
      scopes: client.connect.scopes ?? [],
      getConfig: typeof cfg === "function" ? cfg : () => cfg,
      assertCurrent: () => {},
    }),
  };
}
