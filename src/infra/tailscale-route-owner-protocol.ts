export const TAILSCALE_ROUTE_OWNER_ARG = "--aether-tailscale-route-owner";

export type TailscaleRouteOwnerMessage =
  | { type: "spawned"; pid: number }
  | { type: "ready" }
  | {
      type: "failed";
      code: number | null;
      signal: NodeJS.Signals | null;
      stdout: string;
      stderr: string;
    };
