// Public files serve at "/" during `npm run dev` but under the /assets base
// in the build — Vite never rewrites URLs inside JSX strings, so every
// public-asset reference goes through here.
//
// bump ASSET_V whenever an icon file is replaced in place, so browsers that
// cached the old bytes at the same URL fetch the new ones
export const ASSET_V = "5";

export const assetUrl = (p) => `${import.meta.env.BASE_URL}${p}`;
