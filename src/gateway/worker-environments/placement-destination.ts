import { err, ok, type Result } from "@aether/normalization-core/result";
import { normalizeOptionalString } from "@aether/normalization-core/string-coerce";
import type { AetherConfig } from "../../config/types.aether.js";
import { DEVICE_WORKER_PROVIDER_ID } from "./device-provider-identity.js";
import type { WorkerPlacementDispatchRequest } from "./service-contract.js";

type WorkerPlacementDestination =
  | {
      profileId: string;
      deviceId?: undefined;
      machineClass?: string;
      os?: string;
      inheritedProfile?: undefined;
    }
  | {
      profileId: string;
      deviceId: string;
      inheritedProfile: NonNullable<WorkerPlacementDispatchRequest["inheritedProfile"]>;
    };

export function resolveWorkerPlacementDestination(params: {
  cfg: Pick<AetherConfig, "cloudWorkers">;
  profileId?: string;
  deviceId?: string;
  machineClass?: string;
  os?: string;
}): Result<WorkerPlacementDestination | undefined, string> {
  const profileId = normalizeOptionalString(params.profileId);
  if (profileId) {
    if (!Object.hasOwn(params.cfg.cloudWorkers?.profiles ?? {}, profileId)) {
      return err(`cloud worker profile is not configured: ${profileId}`);
    }
    const machineClass = normalizeOptionalString(params.machineClass);
    if (params.machineClass !== undefined && !machineClass) {
      return err("cloud worker machine class must be non-empty");
    }
    const os = normalizeOptionalString(params.os);
    if (params.os !== undefined && !os) {
      return err("cloud worker operating system must be non-empty");
    }
    return ok({ profileId, ...(machineClass ? { machineClass } : {}), ...(os ? { os } : {}) });
  }
  if (params.os !== undefined || params.machineClass !== undefined) {
    return err("cloud worker machine class and operating system require a profile id");
  }
  const deviceId = normalizeOptionalString(params.deviceId);
  if (!deviceId) {
    return ok(undefined);
  }
  return ok({
    profileId: `device:${deviceId}`,
    deviceId,
    inheritedProfile: {
      providerId: DEVICE_WORKER_PROVIDER_ID,
      profileSnapshot: { install: "bundle", settings: { device: deviceId } },
    },
  });
}
