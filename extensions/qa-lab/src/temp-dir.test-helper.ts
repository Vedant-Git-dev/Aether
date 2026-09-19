// Qa Lab plugin module implements temp dir helper behavior.
import {
  tempWorkspace,
  resolvePreferredAetherTmpDir,
  type TempWorkspace,
} from "aether/plugin-sdk/temp-path";

export function createTempDirHarness() {
  const tempDirs: TempWorkspace[] = [];

  return {
    cleanup: async () => {
      await Promise.all(tempDirs.splice(0).map((dir) => dir.cleanup()));
    },
    makeTempDir: async (prefix: string) => {
      const dir = await tempWorkspace({
        rootDir: resolvePreferredAetherTmpDir(),
        prefix,
      });
      tempDirs.push(dir);
      return dir.dir;
    },
  };
}
