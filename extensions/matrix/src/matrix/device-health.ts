// Matrix plugin module implements device health behavior.
export type MatrixManagedDeviceInfo = {
  deviceId: string;
  displayName: string | null;
  current: boolean;
};

type MatrixDeviceHealthSummary = {
  currentDeviceId: string | null;
  staleAetherDevices: MatrixManagedDeviceInfo[];
  currentAetherDevices: MatrixManagedDeviceInfo[];
};

const AETHER_DEVICE_NAME_PREFIX = "Aether ";

export function isAetherManagedMatrixDevice(displayName: string | null | undefined): boolean {
  return displayName?.startsWith(AETHER_DEVICE_NAME_PREFIX) === true;
}

export function summarizeMatrixDeviceHealth(
  devices: MatrixManagedDeviceInfo[],
): MatrixDeviceHealthSummary {
  const currentDeviceId = devices.find((device) => device.current)?.deviceId ?? null;
  const aetherDevices = devices.filter((device) =>
    isAetherManagedMatrixDevice(device.displayName),
  );
  return {
    currentDeviceId,
    staleAetherDevices: aetherDevices.filter((device) => !device.current),
    currentAetherDevices: aetherDevices.filter((device) => device.current),
  };
}
