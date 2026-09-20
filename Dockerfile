# Opt-in plugin dependencies and supported runtime builds (space- or comma-separated ids).
# Manifest ids and existing source-directory names are accepted.
# Example: docker build --build-arg AETHER_EXTENSIONS="diagnostics-otel,matrix" .
#
# Multi-stage build produces a minimal runtime image without build tools,
# source code, or Bun. Works with Docker, Buildx, and Podman.
# The dependency manifest stages extract only package.json files, so the main
# build layer is not invalidated by unrelated source changes.
#
# Build stages use full bookworm; the runtime image is always bookworm-slim.
ARG AETHER_EXTENSIONS=""
ARG AETHER_BUNDLED_PLUGIN_DIR=extensions
ARG AETHER_DOCKER_BUILD_NODE_OPTIONS="--max-old-space-size=8192"
ARG AETHER_DOCKER_BUILD_TSDOWN_MAX_OLD_SPACE_MB=""
ARG AETHER_DOCKER_BUILD_SKIP_DTS=1
ARG AETHER_NODE_BOOKWORM_IMAGE="docker.io/library/node:24-bookworm@sha256:934240a162082fd8b8a2f90cd5114446443f1eba1c5378f6687167ca405e6584"
ARG AETHER_NODE_BOOKWORM_SLIM_IMAGE="docker.io/library/node:24-bookworm-slim@sha256:3638d9a6fe4030bd716be989438248074489337ba3275657f93595428be4fc03"
ARG AETHER_NODE_BOOKWORM_SLIM_DIGEST="sha256:3638d9a6fe4030bd716be989438248074489337ba3275657f93595428be4fc03"
# Keep in sync with .github/actions/setup-node-env/action.yml bun-version.
# To update: docker buildx imagetools inspect docker.io/oven/bun:<version> and use the manifest-list digest.
ARG AETHER_BUN_IMAGE="docker.io/oven/bun:1.4.0@sha256:5ff609364c049b54eb0ff560ec96319729a972078ef2c755d758f0c6ef89c2d6"

# Base images are pinned to SHA256 digests for reproducible builds.
# Dependabot refreshes these blessed digests; release builds consume the
# reviewed base snapshot instead of mutating distro state on every build.
# To update, run: docker buildx imagetools inspect docker.io/library/node:24-bookworm and
# docker.io/library/node:24-bookworm-slim (or podman) and replace the digests below with the
# current multi-arch manifest list entries.

FROM ${AETHER_NODE_BOOKWORM_IMAGE} AS workspace-deps
ARG AETHER_EXTENSIONS
ARG AETHER_BUNDLED_PLUGIN_DIR
# Copy package.json files for workspace packages used by the install layer.
# Manifest-only bundled plugins remain valid selections but need no workspace metadata.
# Use COPY because build-context bind mounts are unreliable across supported
# Podman/Buildah hosts. Full trees stay in this disposable stage; later stages
# receive only extracted manifests.
COPY scripts/lib/docker-plugin-selection.mjs /tmp/docker-plugin-selection.mjs
COPY scripts/lib/root-package-bundled-plugin-excludes.mjs /tmp/root-package-bundled-plugin-excludes.mjs
COPY package.json /tmp/package.json
COPY packages /tmp/packages
COPY ${AETHER_BUNDLED_PLUGIN_DIR} /tmp/${AETHER_BUNDLED_PLUGIN_DIR}
RUN mkdir -p /out/packages "/out/${AETHER_BUNDLED_PLUGIN_DIR}" && \
    for manifest in /tmp/packages/*/package.json; do \
      [ -f "$manifest" ] || continue; \
      pkg_dir="${manifest%/package.json}"; \
      pkg_name="${pkg_dir##*/}"; \
      mkdir -p "/out/packages/$pkg_name" && \
      cp "$manifest" "/out/packages/$pkg_name/package.json"; \
    done && \
    node /tmp/docker-plugin-selection.mjs "/tmp/${AETHER_BUNDLED_PLUGIN_DIR}" "$AETHER_EXTENSIONS" \
      > /out/aether-selected-plugin-dirs && \
    node /tmp/docker-plugin-selection.mjs "/tmp/${AETHER_BUNDLED_PLUGIN_DIR}" "$AETHER_EXTENSIONS" \
      --required-platform-packages > /out/aether-required-platform-packages && \
    node /tmp/docker-plugin-selection.mjs "/tmp/${AETHER_BUNDLED_PLUGIN_DIR}" "$AETHER_EXTENSIONS" \
      --required-bundled /tmp/package.json > /tmp/aether-workspace-plugin-dirs && \
    while IFS= read -r ext; do \
      ext_dir="/tmp/${AETHER_BUNDLED_PLUGIN_DIR}/$ext"; \
      if [ -f "$ext_dir/package.json" ]; then \
        mkdir -p "/out/${AETHER_BUNDLED_PLUGIN_DIR}/$ext" && \
        cp "$ext_dir/package.json" "/out/${AETHER_BUNDLED_PLUGIN_DIR}/$ext/package.json"; \
      fi; \
    done < /tmp/aether-workspace-plugin-dirs

# Shared manifest-only inputs. Both installs start without node_modules so pnpm
# never has to rename a dependency directory inherited from an OverlayFS layer.
FROM ${AETHER_NODE_BOOKWORM_IMAGE} AS dependency-inputs
ARG AETHER_BUNDLED_PLUGIN_DIR

RUN corepack enable

WORKDIR /app

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml .npmrc ./
COPY node-version.mjs ./
COPY node-sqlite.mjs ./
COPY node-runtime-update.mjs ./
COPY node-runtime-recovery.mjs ./
COPY aether.mjs ./
COPY ui/package.json ./ui/package.json
COPY patches ./patches
COPY scripts/postinstall-bundled-plugins.mjs scripts/preinstall-package-manager-warning.mjs scripts/windows-cmd-helpers.mjs scripts/prepare-git-hooks.mjs scripts/check-install-dependency-ownership.mjs ./scripts/
COPY scripts/lib/guard-inventory-utils.mjs ./scripts/lib/guard-inventory-utils.mjs
COPY scripts/lib/package-dist-imports.mjs ./scripts/lib/package-dist-imports.mjs
COPY scripts/lib/package-lifecycle-marker.mjs ./scripts/lib/package-lifecycle-marker.mjs
COPY scripts/docker/verify-fs-safe-native.mjs ./scripts/docker/verify-fs-safe-native.mjs
COPY scripts/docker/verify-native-addons.sh ./scripts/docker/verify-native-addons.sh

COPY --from=workspace-deps /out/packages/ ./packages/
COPY --from=workspace-deps /out/${AETHER_BUNDLED_PLUGIN_DIR}/ ./${AETHER_BUNDLED_PLUGIN_DIR}/
COPY --from=workspace-deps /out/aether-selected-plugin-dirs /tmp/aether-selected-plugin-dirs
COPY --from=workspace-deps /out/aether-required-platform-packages /tmp/aether-required-platform-packages

# ── Production dependencies ────────────────────────────────────
FROM dependency-inputs AS production-deps
RUN --mount=type=cache,id=aether-pnpm-store,target=/root/.local/share/pnpm/store,sharing=locked \
    NODE_OPTIONS=--max-old-space-size=2048 pnpm install --frozen-lockfile --prod \
      --config.supportedArchitectures.os=linux \
      --config.supportedArchitectures.cpu="$(node -p 'process.arch')" \
      --config.supportedArchitectures.libc=glibc
RUN sh scripts/docker/verify-native-addons.sh

# ── Build ──────────────────────────────────────────────────────
FROM ${AETHER_BUN_IMAGE} AS bun-binary
FROM dependency-inputs AS build
ARG AETHER_DOCKER_BUILD_NODE_OPTIONS
ARG AETHER_DOCKER_BUILD_TSDOWN_MAX_OLD_SPACE_MB
ARG AETHER_DOCKER_BUILD_SKIP_DTS

# Copy pinned Bun binary from the official image instead of fetching via curl.
COPY --from=bun-binary /usr/local/bin/bun /usr/local/bin/bun

# Reduce OOM risk on low-memory hosts during dependency installation.
# Docker builds on small VMs may otherwise fail with "Killed" (exit 137).
RUN --mount=type=cache,id=aether-pnpm-store,target=/root/.local/share/pnpm/store,sharing=locked \
    NODE_OPTIONS=--max-old-space-size=2048 pnpm install --frozen-lockfile \
      --config.supportedArchitectures.os=linux \
      --config.supportedArchitectures.cpu="$(node -p 'process.arch')" \
      --config.supportedArchitectures.libc=glibc

RUN sh scripts/docker/verify-native-addons.sh

# Public source provenance supplied by release automation or local setup. Keep
# these after the dependency layer so a new timestamp does not invalidate install.
ARG GIT_COMMIT=""
ARG AETHER_BUILD_TIMESTAMP=""
ARG AETHER_DOCKER_BUILD_VERSION=""
ENV GIT_COMMIT=${GIT_COMMIT} \
    AETHER_BUILD_TIMESTAMP=${AETHER_BUILD_TIMESTAMP}

COPY . .

# The build stage also backs non-root live-test containers. Build contexts preserve
# host modes, so normalize copied source readability without re-walking installed deps.
RUN find /app -path /app/node_modules -prune -o -exec chmod a+rX {} +

# Normalize extension paths now so runtime COPY preserves safe modes
# without adding a second full extensions layer.
RUN for dir in /app/${AETHER_BUNDLED_PLUGIN_DIR} /app/.agent /app/.agents; do \
      if [ -d "$dir" ]; then \
        find "$dir" -type d -exec chmod 755 {} +; \
        find "$dir" -type f -exec chmod 644 {} +; \
      fi; \
    done

# A2UI bundle may fail under QEMU cross-compilation (e.g. building amd64
# on Apple Silicon). CI builds natively per-arch so this is a no-op there.
# Stub it so local cross-arch builds still succeed.
RUN pnpm_config_verify_deps_before_run=false pnpm canvas:a2ui:bundle || \
    (echo "A2UI bundle: creating stub (non-fatal)" && \
     mkdir -p extensions/canvas/src/host/a2ui && \
     echo "/* A2UI bundle unavailable in this build */" > extensions/canvas/src/host/a2ui/a2ui.bundle.js && \
     echo "stub" > extensions/canvas/src/host/a2ui/.bundle.hash && \
     rm -rf vendor/a2ui apps/shared/AetherKit/Tools/CanvasA2UI)
# Force pnpm for UI build (Bun may fail on ARM/Synology architectures)
ENV AETHER_PREFER_PNPM=1
# Correction-release sources keep the base package version; official images
# stamp the complete release version before generating their build metadata.
RUN set -eu; \
    selected_plugin_dirs="$(cat /tmp/aether-selected-plugin-dirs)"; \
    if [ -n "$AETHER_DOCKER_BUILD_VERSION" ]; then \
      pnpm pkg set "version=$AETHER_DOCKER_BUILD_VERSION"; \
    fi; \
    if [ -z "$AETHER_BUILD_TIMESTAMP" ]; then \
      AETHER_BUILD_TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"; \
      export AETHER_BUILD_TIMESTAMP; \
    fi; \
    if grep -qx 'qa-lab' /tmp/aether-selected-plugin-dirs; then \
      export AETHER_BUILD_PRIVATE_QA=1 AETHER_ENABLE_PRIVATE_QA_CLI=1; \
    fi; \
    AETHER_INTERNAL_DOCKER_BUILD_PLUGIN_IDS="$selected_plugin_dirs" AETHER_RUN_NODE_SKIP_DTS_BUILD="$AETHER_DOCKER_BUILD_SKIP_DTS" AETHER_TSDOWN_MAX_OLD_SPACE_MB="$AETHER_DOCKER_BUILD_TSDOWN_MAX_OLD_SPACE_MB" NODE_OPTIONS="$AETHER_DOCKER_BUILD_NODE_OPTIONS" pnpm_config_verify_deps_before_run=false pnpm build:docker; \
    pnpm_config_verify_deps_before_run=false pnpm ui:build
RUN if grep -qx 'qa-lab' /tmp/aether-selected-plugin-dirs; then \
      pnpm_config_verify_deps_before_run=false pnpm qa:lab:build && \
      mkdir -p dist/extensions/qa-lab/web && \
      rm -rf dist/extensions/qa-lab/web/dist && \
      cp -R extensions/qa-lab/web/dist dist/extensions/qa-lab/web/dist; \
    fi

# Keep compiled workspaces and generated plugin assets, but omit development
# dependency trees before merging the build output into the production install.
FROM build AS runtime-build-output
ARG AETHER_BUNDLED_PLUGIN_DIR
RUN rm -rf node_modules ui/node_modules && \
    find packages "${AETHER_BUNDLED_PLUGIN_DIR}" -name node_modules -prune -exec rm -rf {} +

# Inherit production dependencies instead of copying their full tree again.
# The build overlay also carries the stamped release package.json.
FROM production-deps AS runtime-assets
ARG AETHER_BUNDLED_PLUGIN_DIR
COPY --from=runtime-build-output /app/ ./

# Prune omitted plugins and build metadata, then link the selected plugins'
# plugin-local dependencies under dist/extensions/<id> after package lifecycle
# cleanup so the packaged roots keep them. Keep SDK-native binaries only for
# selected plugins that explicitly require them.
RUN node scripts/postinstall-bundled-plugins.mjs && \
    AETHER_EXTENSIONS="$(cat /tmp/aether-selected-plugin-dirs)" AETHER_BUNDLED_PLUGIN_DIR="$AETHER_BUNDLED_PLUGIN_DIR" node scripts/prune-docker-plugin-dist.mjs && \
    find dist -type f \( -name '*.d.ts' -o -name '*.d.mts' -o -name '*.d.cts' -o -name '*.map' \) -delete && \
    if [ -L /app/node_modules/@aether/ai ]; then \
      ai_runtime_target="$(readlink -f /app/node_modules/@aether/ai)" && \
      ai_runtime_tmp="$(mktemp -d)" && \
      cp -a "$ai_runtime_target" "$ai_runtime_tmp/ai" && \
      rm /app/node_modules/@aether/ai && \
      mv "$ai_runtime_tmp/ai" /app/node_modules/@aether/ai && \
      rmdir "$ai_runtime_tmp"; \
    fi && \
    rm -rf \
      /app/node_modules/aether \
      /app/node_modules/.bin/aether \
      /app/node_modules/.pnpm/aether@*/node_modules/aether && \
    if ! grep -q '^@anthropic-ai/claude-agent-sdk-' /tmp/aether-required-platform-packages; then \
      find /app/node_modules/@anthropic-ai -maxdepth 1 -type d \
        -name 'claude-agent-sdk-linux-*' -exec rm -rf {} +; \
    fi && \
    node --input-type=module -e 'await import("grammy")' && \
    node scripts/check-package-dist-imports.mjs /app

# ── Runtime base image ──────────────────────────────────────────
FROM ${AETHER_NODE_BOOKWORM_SLIM_IMAGE} AS base-runtime
ARG AETHER_NODE_BOOKWORM_SLIM_DIGEST
LABEL org.opencontainers.image.base.name="docker.io/library/node:24-bookworm-slim" \
  org.opencontainers.image.base.digest="${AETHER_NODE_BOOKWORM_SLIM_DIGEST}"

# ── Stage 3: Runtime ────────────────────────────────────────────
FROM base-runtime
ARG AETHER_BUNDLED_PLUGIN_DIR

# OCI base-image metadata for downstream image consumers.
# If you change these annotations, also update:
# - docs/install/docker.md ("Base image metadata" section)
# - https://docs.aether.ai/install/docker
LABEL org.opencontainers.image.source="https://github.com/aether/aether" \
  org.opencontainers.image.url="https://aether.ai" \
  org.opencontainers.image.documentation="https://docs.aether.ai/install/docker" \
  org.opencontainers.image.licenses="MIT" \
  org.opencontainers.image.title="Aether" \
  org.opencontainers.image.description="Aether gateway and CLI runtime container image"

WORKDIR /app

# Install missing runtime utilities and the OpenMP library required by llama-server.
# `ca-certificates` ships in `bookworm` (full) but not in `bookworm-slim`,
# so it must be installed explicitly here. Without it `/etc/ssl/certs/`
# stays empty and every HTTPS outbound dies at TLS handshake with
# `error setting certificate file`.
# The runtime image must include the SSH client because the sandbox backend
# spawns `ssh` directly, and bookworm-slim does not provide it.
# Apply current Debian point-release security fixes even when the pinned base
# digest predates them, without waiting for a base-digest refresh.
RUN --mount=type=cache,id=aether-bookworm-apt-cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=aether-bookworm-apt-lists,target=/var/lib/apt,sharing=locked \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get dist-upgrade -y && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates curl git hostname libgomp1 lsof openssh-client openssl procps python3 tini && \
    update-ca-certificates

# Keep npm as an operator-facing capability while replacing the base image's
# bundled CLI dependency tree with the current release. The published package
# omits its dev tools, so hide their metadata during the script-free refresh.
RUN npm install --global npm@latest && \
    npm_dir="$(npm root --global)/npm" && \
    cp "$npm_dir/package.json" /tmp/npm-package.json && \
    node -e 'const fs = require("node:fs"); const file = process.argv[1]; const packageJson = JSON.parse(fs.readFileSync(file, "utf8")); delete packageJson.devDependencies; fs.writeFileSync(file, `${JSON.stringify(packageJson, null, 2)}\n`);' "$npm_dir/package.json" && \
    npm update --prefix "$npm_dir" --omit=dev --ignore-scripts --no-audit --no-fund && \
    mv /tmp/npm-package.json "$npm_dir/package.json" && \
    npm cache clean --force

RUN chown node:node /app

COPY --from=runtime-assets --chown=node:node /app/dist ./dist
COPY --from=runtime-assets --chown=node:node /app/node_modules ./node_modules
COPY --from=runtime-assets --chown=node:node /app/package.json .
COPY --from=runtime-assets --chown=node:node /app/pnpm-lock.yaml .
COPY --from=runtime-assets --chown=node:node /app/pnpm-workspace.yaml .
COPY --from=runtime-assets --chown=node:node /app/patches ./patches
COPY --from=runtime-assets --chown=node:node /app/node-version.mjs .
COPY --from=runtime-assets --chown=node:node /app/node-sqlite.mjs .
COPY --from=runtime-assets --chown=node:node /app/node-runtime-update.mjs .
COPY --from=runtime-assets --chown=node:node /app/node-runtime-recovery.mjs .
COPY --from=runtime-assets --chown=node:node /app/aether.mjs .
COPY --from=runtime-assets --chown=node:node /app/${AETHER_BUNDLED_PLUGIN_DIR} ./${AETHER_BUNDLED_PLUGIN_DIR}
COPY --from=runtime-assets --chown=node:node /app/skills ./skills
COPY --from=runtime-assets --chown=node:node /app/docs ./docs
COPY --from=runtime-assets --chown=node:node /app/qa ./qa
RUN --mount=from=dependency-inputs,source=/app/scripts/docker/verify-fs-safe-native.mjs,target=/tmp/verify-fs-safe-native.mjs \
    node /tmp/verify-fs-safe-native.mjs --package-root /app --mode require

# Validate the three version surfaces in every release-built runtime variant.
ARG AETHER_DOCKER_BUILD_VERSION
RUN if [ -n "$AETHER_DOCKER_BUILD_VERSION" ]; then \
      test "$(node -p "require(\"/app/package.json\").version")" = "$AETHER_DOCKER_BUILD_VERSION"; \
      test "$(node -p "require(\"/app/dist/build-info.json\").version")" = "$AETHER_DOCKER_BUILD_VERSION"; \
      test "$(node /app/aether.mjs --version | cut -d ' ' -f 2)" = "$AETHER_DOCKER_BUILD_VERSION"; \
    fi

# Keep pnpm available in the runtime image for container-local workflows.
# Use a shared Corepack home so the non-root `node` user does not need a
# first-run network fetch when invoking pnpm. Warm in /app to also record
# its pinned toolchain metadata before offline runtime use.
ENV COREPACK_HOME=/usr/local/share/corepack
RUN install -d -m 0755 "$COREPACK_HOME" && \
    corepack enable && \
    pnpm_spec="$(node -p "require('./package.json').packageManager")" && \
    for attempt in 1 2 3 4 5; do \
      if corepack prepare "$pnpm_spec" --activate; then \
        break; \
      fi; \
      if [ "$attempt" -eq 5 ]; then \
        exit 1; \
      fi; \
      sleep $((attempt * 2)); \
    done && \
    corepack "$pnpm_spec" --version && \
    chmod a+r /app/pnpm-lock.yaml && \
    chmod -R a+rX "$COREPACK_HOME"

# Install additional system packages needed by your skills or extensions.
# Example: docker build --build-arg AETHER_IMAGE_APT_PACKAGES="python3 wget" .
# Legacy alias: AETHER_DOCKER_APT_PACKAGES is still accepted as a fallback.
ARG AETHER_IMAGE_APT_PACKAGES
ARG AETHER_DOCKER_APT_PACKAGES=""
ENV PATH="/home/node/.local/bin:${PATH}"
RUN --mount=type=cache,id=aether-bookworm-apt-cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=aether-bookworm-apt-lists,target=/var/lib/apt,sharing=locked \
    packages="${AETHER_IMAGE_APT_PACKAGES:-$AETHER_DOCKER_APT_PACKAGES}"; \
    if [ -n "$packages" ]; then \
      apt-get update && \
      DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $packages; \
    fi

# Install additional Python packages needed by your plugins or skills.
# Example: docker build --build-arg AETHER_IMAGE_PIP_PACKAGES="requests humanize" .
ARG AETHER_IMAGE_PIP_PACKAGES=""
RUN --mount=type=cache,id=aether-bookworm-apt-cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=aether-bookworm-apt-lists,target=/var/lib/apt,sharing=locked \
    if [ -n "$AETHER_IMAGE_PIP_PACKAGES" ]; then \
      if ! python3 -m pip --version >/dev/null 2>&1; then \
        apt-get update && \
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3-pip; \
      fi && \
      python3 -m pip install --no-cache-dir --break-system-packages $AETHER_IMAGE_PIP_PACKAGES; \
    fi

# Optionally install Chromium and Xvfb for browser automation.
# Build with: docker build --build-arg AETHER_INSTALL_BROWSER=1 ...
# Adds ~300MB but eliminates the 60-90s Playwright install on every container start.
# Must run after node_modules COPY so playwright-core is available.
ARG AETHER_INSTALL_BROWSER=""
ENV PLAYWRIGHT_BROWSERS_PATH=/home/node/.cache/ms-playwright
RUN --mount=type=cache,id=aether-bookworm-apt-cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=aether-bookworm-apt-lists,target=/var/lib/apt,sharing=locked \
    if [ -n "$AETHER_INSTALL_BROWSER" ]; then \
      apt-get update && \
      DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends xvfb && \
      install -d -m 0755 -o node -g node "$(dirname "$PLAYWRIGHT_BROWSERS_PATH")" && \
      mkdir -p "$PLAYWRIGHT_BROWSERS_PATH" && \
      node /app/node_modules/playwright-core/cli.js install --with-deps chromium && \
      chown -R node:node "$PLAYWRIGHT_BROWSERS_PATH"; \
    fi

# Optionally install Docker CLI for sandbox container management.
# Build with: docker build --build-arg AETHER_INSTALL_DOCKER_CLI=1 ...
# Adds ~50MB. Only the CLI is installed — no Docker daemon.
# Required for agents.defaults.sandbox to function in Docker deployments.
ARG AETHER_INSTALL_DOCKER_CLI=""
ARG AETHER_DOCKER_GPG_FINGERPRINT="9DC858229FC7DD38854AE2D88D81803C0EBFCD88"
RUN --mount=type=cache,id=aether-bookworm-apt-cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=aether-bookworm-apt-lists,target=/var/lib/apt,sharing=locked \
    if [ -n "$AETHER_INSTALL_DOCKER_CLI" ]; then \
      apt-get update && \
      DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg && \
      install -m 0755 -d /etc/apt/keyrings && \
      # Verify Docker apt signing key fingerprint before trusting it as a root key.
      # Require exactly one primary key (`pub` in --with-colons; subkeys use `sub`) so we
      # never pin the first fingerprint while apt trusts extra keys from the same file.
      # Update AETHER_DOCKER_GPG_FINGERPRINT when Docker rotates release keys.
      curl -fsSL --connect-timeout 10 --max-time 120 \
        https://download.docker.com/linux/debian/gpg -o /tmp/docker.gpg.asc && \
      expected_fingerprint="$(printf '%s' "$AETHER_DOCKER_GPG_FINGERPRINT" | tr '[:lower:]' '[:upper:]' | tr -d '[:space:]')" && \
      docker_gpg_pub_count="$(gpg --batch --show-keys --with-colons /tmp/docker.gpg.asc | awk -F: '$1 == "pub" { c++ } END { print c+0 }')" && \
      if [ "$docker_gpg_pub_count" != "1" ]; then \
        echo "ERROR: Docker apt key must contain exactly one public key (found $docker_gpg_pub_count); refusing a multi-key file." >&2; \
        exit 1; \
      fi && \
      actual_fingerprint="$(gpg --batch --show-keys --with-colons /tmp/docker.gpg.asc | awk -F: '$1 == "fpr" { print toupper($10); exit }')" && \
      if [ -z "$actual_fingerprint" ] || [ "$actual_fingerprint" != "$expected_fingerprint" ]; then \
        echo "ERROR: Docker apt key fingerprint mismatch (expected $expected_fingerprint, got ${actual_fingerprint:-<empty>})" >&2; \
        exit 1; \
      fi && \
      gpg --dearmor -o /etc/apt/keyrings/docker.gpg /tmp/docker.gpg.asc && \
      rm -f /tmp/docker.gpg.asc && \
      chmod a+r /etc/apt/keyrings/docker.gpg && \
      printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable\n' \
        "$(dpkg --print-architecture)" > /etc/apt/sources.list.d/docker.list && \
      apt-get update && \
      DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        docker-ce-cli docker-compose-plugin; \
    fi

# Expose the CLI binary without requiring npm global writes as non-root.
RUN ln -sf /app/aether.mjs /usr/local/bin/aether \
 && chmod 755 /app/aether.mjs

# Pre-create default named-volume mount points so first-run Docker volumes copy
# node ownership from the image instead of starting as root-owned directories.
# NOTE: /home/node/.config must be created with node ownership first so that
# the leaf /home/node/.config/aether inherits the correct parent permissions.
# Without this, install -d leaves /home/node/.config as root:root (issue #85968).
RUN install -d -m 0755 -o node -g node /home/node/.config && \
    install -d -m 0700 -o node -g node \
      /home/node/.aether \
      /home/node/.aether/workspace \
      /home/node/.config/aether && \
    stat -c '%U:%G %a' /home/node/.aether | grep -qx 'node:node 700' && \
    stat -c '%U:%G %a' /home/node/.aether/workspace | grep -qx 'node:node 700' && \
    stat -c '%U:%G %a' /home/node/.config | grep -qx 'node:node 755' && \
    stat -c '%U:%G %a' /home/node/.config/aether | grep -qx 'node:node 700'

ENV NODE_ENV=production

# Security hardening: Run as non-root user
# The node:24-bookworm image includes a 'node' user (uid 1000)
# This reduces the attack surface by preventing container escape via root privileges
USER node

# Verify the shipped toolchain needs no privileged writes or first-run downloads.
RUN COREPACK_ENABLE_NETWORK=0 PNPM_CONFIG_OFFLINE=true pnpm --version

# Start gateway server with default config.
# Binds to loopback (127.0.0.1) by default for security.
#
# IMPORTANT: With Docker bridge networking (-p 18789:18789), loopback bind
# makes the gateway unreachable from the host. Either:
#   - Use --network host, OR
#   - Override --bind to "lan" (0.0.0.0) and set auth credentials
#
# Built-in probe endpoints for container health checks:
#   - GET /healthz (liveness), GET /startupz (startup/traffic admission),
#     and GET /readyz (channel-aware readiness)
#   - aliases: /health, /startup, and /ready
# For external access from host/ingress, override bind to "lan" and set auth.
HEALTHCHECK --interval=3m --timeout=10s --start-period=15s --retries=3 \
  CMD ["node", "dist/docker-healthcheck.js"]
ENTRYPOINT ["tini", "-s", "--"]
CMD ["node", "aether.mjs", "gateway"]
