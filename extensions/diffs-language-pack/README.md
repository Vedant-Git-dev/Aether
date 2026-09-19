# @aether/diffs-language-pack

Official extended syntax highlighting pack for the Aether Diffs plugin.

The base `@aether/diffs` plugin ships a curated language set. Install this package when you want the full Shiki language catalog available in rendered diff viewers and diff image/PDF output.

The pack adds highlighting for languages outside the default diffs viewer set, including Astro, Vue, Svelte, MDX, GraphQL, Terraform/HCL, Nix, Clojure, Elixir, Haskell, OCaml, Scala, Zig, Solidity, Verilog/VHDL, Fortran, MATLAB, LaTeX, Mermaid, Sass/Less/SCSS, Nginx, Apache, CSV, dotenv, INI, and diff files. See the plugin reference and Shiki language catalog for details.

## Install

```bash
aether plugins install @aether/diffs-language-pack
```

Restart the Gateway after installing or updating the plugin.

## Use with Diffs

Install `@aether/diffs` first, then install this language pack. The language pack contributes static viewer assets; it does not register a separate agent tool.

## Docs

- https://docs.aether.ai/tools/diffs
- https://docs.aether.ai/plugins/reference/diffs-language-pack

## Package

- Plugin id: `diffs-language-pack`
- Package: `@aether/diffs-language-pack`
- Minimum Aether host: `2026.5.27`
