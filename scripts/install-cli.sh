#!/usr/bin/env bash
# Aether installer for Linux and macOS.
#
# Full setup (default):
#   curl -fsSL https://aether.org/install.sh | bash
#   Fetches Aether, installs its dependencies, and drops you into the
#   interactive first-run experience.
#
# Runtime-only mode (invoked by Aether's self-healing runtime recovery;
# leaves the app untouched):
#   ./scripts/install-cli.sh --node-only --prefix <dir>
#
# Flags:
#   --dir <path>     Where Aether lives (default: ~/.aether/app)
#   --node-only      Provision a compatible Node.js runtime and stop
#   --prefix <dir>   With --node-only: target directory for the runtime
#   --no-setup       Install without launching first-run
#   -h, --help       Print this text

set -euo pipefail

AETHER_REPO="${AETHER_REPO:-https://github.com/aether/aether.git}"
AETHER_MIN_NODE_MAJOR=22
DEFAULT_DIR="${HOME}/.aether/app"

INSTALL_DIR="$DEFAULT_DIR"
NODE_ONLY=0
NODE_PREFIX=""
RUN_SETUP=1

say()  { printf '\033[35m[aether]\033[0m %s\n' "$*"; }
note() { printf '\033[33m[aether]\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[31m[aether]\033[0m %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dir)       INSTALL_DIR="${2:?--dir expects a path}"; shift 2 ;;
    --node-only) NODE_ONLY=1; shift ;;
    --prefix)    NODE_PREFIX="${2:?--prefix expects a path}"; shift 2 ;;
    --no-setup)  RUN_SETUP=0; shift ;;
    -h|--help)   usage; exit 0 ;;
    *) fail "Unrecognized flag: $1 (see --help)" ;;
  esac
done

have() { command -v "$1" >/dev/null 2>&1; }

node_major() { node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0; }

node_ready() {
  have node && [ "$(node_major)" -ge "$AETHER_MIN_NODE_MAJOR" ] 2>/dev/null
}

provision_node() {
  # Private, per-user Node.js built from the official prebuilt archives.
  # System Node, package managers, and shell profiles are never touched.
  local prefix="${1:-${HOME}/.aether/node}"
  local platform arch archive url tmp
  platform="$(uname -s)"; arch="$(uname -m)"
  case "$platform" in
    Linux)  platform="linux" ;;
    Darwin) platform="darwin" ;;
    *) fail "Unsupported platform: $platform" ;;
  esac
  case "$arch" in
    x86_64|amd64)  arch="x64" ;;
    arm64|aarch64) arch="arm64" ;;
    *) fail "Unsupported architecture: $arch" ;;
  esac

  # Find the newest LTS line from the official release index.
  have curl || fail "curl is needed to fetch Node.js"
  local version
  version="$(curl -fsSL https://nodejs.org/dist/index.json \
    | grep -m1 '"lts":' \
    | sed 's/.*"version":"\([^"]*\)".*/\1/')"
  [ -n "$version" ] || fail "Couldn't look up the current Node.js LTS release"
  say "Fetching Node.js $version ($platform-$arch)..."

  archive="node-${version}-${platform}-${arch}.tar.gz"
  url="https://nodejs.org/dist/${version}/${archive}"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN
  curl -fsSL "$url" -o "$tmp/$archive" || fail "Node.js download failed: $url"
  mkdir -p "$prefix"
  tar -xzf "$tmp/$archive" -C "$prefix" --strip-components=1
  say "Node.js is ready at $prefix"
  printf '%s\n' "$prefix"
}

require_node() {
  if node_ready; then
    say "Found Node.js $(node -v)."
    return 0
  fi
  if [ "$NODE_ONLY" = 1 ]; then
    provision_node "$NODE_PREFIX" >/dev/null
    return 0
  fi
  note "Node.js ${AETHER_MIN_NODE_MAJOR}+ isn't available yet."
  provision_node "${HOME}/.aether/node" >/dev/null
  export PATH="${HOME}/.aether/node/bin:$PATH"
  node_ready || fail "The fresh Node.js runtime didn't come up"
  say "Using a private Node.js at ${HOME}/.aether/node (put its bin/ on your PATH)."
}

require_pnpm() {
  if have pnpm; then
    say "Found pnpm $(pnpm -v)."
    return 0
  fi
  say "Bringing in pnpm through corepack..."
  if have corepack; then
    corepack enable >/dev/null 2>&1 || true
    corepack prepare pnpm@latest --activate || fail "corepack couldn't activate pnpm"
  else
    npm install -g pnpm || fail "Couldn't install pnpm"
  fi
  have pnpm || fail "pnpm still isn't on PATH"
}

fetch_aether() {
  if [ -f "$INSTALL_DIR/package.json" ]; then
    say "Aether already lives at $INSTALL_DIR — refreshing it."
    git -C "$INSTALL_DIR" pull --ff-only || note "Couldn't pull; keeping the existing copy"
    return 0
  fi
  have git || fail "git is needed to fetch Aether"
  say "Fetching Aether into $INSTALL_DIR ..."
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone --depth 1 "$AETHER_REPO" "$INSTALL_DIR" || fail "Clone failed: $AETHER_REPO"
}

install_deps() {
  say "Installing dependencies (grab a coffee — first run takes a few minutes)..."
  (cd "$INSTALL_DIR" && pnpm install) || fail "pnpm install failed"
}

first_run() {
  say "Handing you over to first-run setup..."
  cd "$INSTALL_DIR"
  exec node scripts/run-app.mjs ignite
}

# --- main -------------------------------------------------------------------
if [ "$NODE_ONLY" = 1 ]; then
  if node_ready; then exit 0; fi
  provision_node "$NODE_PREFIX" >/dev/null
  exit 0
fi

say "Welcome to Aether — Linux/macOS edition"
require_node
require_pnpm
fetch_aether
install_deps

cat <<EOF

  Aether lives at: $INSTALL_DIR

  Start it whenever you like:
    cd "$INSTALL_DIR" && pnpm start

EOF

if [ "$RUN_SETUP" = 1 ]; then
  first_run
else
  say "First-run skipped (--no-setup). Whenever you're ready: node scripts/run-app.mjs ignite"
fi
