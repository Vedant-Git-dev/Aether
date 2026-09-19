export async function resolveConfiguredWorkspaces(
  config: unknown,
  env: NodeJS.ProcessEnv,
): Promise<string[]> {
  const { resolveMemoryDreamingWorkspaces } =
    await import("aether/plugin-sdk/memory-core-host-status");
  return resolveMemoryDreamingWorkspaces(
    config as Parameters<typeof resolveMemoryDreamingWorkspaces>[0],
    { env },
  ).map((entry) => entry.workspaceDir);
}
