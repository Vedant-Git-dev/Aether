export type AetherSchemaVersions = {
  state: number;
  agent: number;
};

export function parseAetherSchemaVersions(value: unknown): AetherSchemaVersions | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return undefined;
  }
  const record = value as Record<string, unknown>;
  if (
    !Number.isInteger(record.state) ||
    (record.state as number) < 0 ||
    !Number.isInteger(record.agent) ||
    (record.agent as number) < 0
  ) {
    return undefined;
  }
  return { state: record.state as number, agent: record.agent as number };
}

export function parsePackageAetherSchemaVersions(
  packageJson: unknown,
): AetherSchemaVersions | undefined {
  if (!packageJson || typeof packageJson !== "object" || Array.isArray(packageJson)) {
    return undefined;
  }
  const aether = (packageJson as Record<string, unknown>).aether;
  if (!aether || typeof aether !== "object" || Array.isArray(aether)) {
    return undefined;
  }
  return parseAetherSchemaVersions((aether as Record<string, unknown>).schemaVersions);
}
