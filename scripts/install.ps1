# Aether installer for Windows.
#
# Full setup (default):
#   irm https://aether.org/install.ps1 | iex
#   Fetches Aether, installs its dependencies, and drops you into the
#   interactive first-run experience.
#
# Runtime-only mode (invoked by Aether's self-healing runtime recovery;
# leaves the app untouched):
#   .\scripts\install.ps1 -NodeOnly -NodePrefix <dir>
#
# Parameters:
#   -Dir <path>         Where Aether lives (default: $env:USERPROFILE\.aether\app)
#   -NodeOnly           Provision a compatible Node.js runtime and stop
#   -NodePrefix <dir>   With -NodeOnly: target directory for the runtime
#   -NoSetup            Install without launching first-run

[CmdletBinding()]
param(
  [string]$Dir = (Join-Path $env:USERPROFILE '.aether\app'),
  [switch]$NodeOnly,
  [Alias('Prefix')]
  [string]$NodePrefix = (Join-Path $env:USERPROFILE '.aether\node'),
  [switch]$NoSetup
)

$ErrorActionPreference = 'Stop'

$AetherRepo   = if ($env:AETHER_REPO) { $env:AETHER_REPO } else { 'https://github.com/aether/aether.git' }
$MinNodeMajor = 22

function Say([string]$msg)  { Write-Host "[aether] $msg" -ForegroundColor Magenta }
function Note([string]$msg) { Write-Host "[aether] $msg" -ForegroundColor Yellow }
function Fail([string]$msg) { Write-Host "[aether] $msg" -ForegroundColor Red; exit 1 }

function Test-NodeReady {
  $node = Get-Command node -ErrorAction SilentlyContinue
  if (-not $node) { return $false }
  try {
    $major = [int](node -p "process.versions.node.split('.')[0]")
    return $major -ge $MinNodeMajor
  } catch { return $false }
}

function Install-Node([string]$TargetPrefix) {
  # Private, per-user Node.js from the official zip. System Node, the
  # registry, and PATH are never touched.
  $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'x64' }
  Say 'Looking up the current Node.js LTS release...'
  $index = Invoke-RestMethod 'https://nodejs.org/dist/index.json'
  $lts = $index | Where-Object { $_.lts } | Select-Object -First 1
  if (-not $lts) { Fail "Couldn't look up the current Node.js LTS release" }
  $version = $lts.version
  Say "Fetching Node.js $version (win-$arch)..."

  $zip = "node-$version-win-$arch.zip"
  $url = "https://nodejs.org/dist/$version/$zip"
  $tmp = Join-Path ([IO.Path]::GetTempPath()) ([IO.Path]::GetRandomFileName())
  New-Item -ItemType Directory -Path $tmp | Out-Null
  try {
    Invoke-WebRequest -Uri $url -OutFile (Join-Path $tmp $zip)
    New-Item -ItemType Directory -Force -Path $TargetPrefix | Out-Null
    Expand-Archive -Path (Join-Path $tmp $zip) -DestinationPath $tmp
    Copy-Item -Recurse -Force (Join-Path $tmp "node-$version-win-$arch\*") $TargetPrefix
  } finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
  }
  Say "Node.js is ready at $TargetPrefix"
}

function Require-Node {
  if (Test-NodeReady) {
    Say "Found Node.js $(node -v)."
    return
  }
  Note "Node.js $MinNodeMajor+ isn't available yet."
  Install-Node $NodePrefix
  $env:PATH = "$NodePrefix;$env:PATH"
  if (-not (Test-NodeReady)) { Fail "The fresh Node.js runtime didn't come up" }
  Say "Using a private Node.js at $NodePrefix (add it to your PATH for future sessions)."
}

function Require-Pnpm {
  if (Get-Command pnpm -ErrorAction SilentlyContinue) {
    Say "Found pnpm $(pnpm -v)."
    return
  }
  Say 'Bringing in pnpm through corepack...'
  if (Get-Command corepack -ErrorAction SilentlyContinue) {
    corepack enable 2>$null
    corepack prepare pnpm@latest --activate
    if ($LASTEXITCODE -ne 0) { Fail "corepack couldn't activate pnpm" }
  } else {
    npm install -g pnpm
    if ($LASTEXITCODE -ne 0) { Fail "Couldn't install pnpm" }
  }
  if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) { Fail "pnpm still isn't on PATH" }
}

function Get-Aether {
  if (Test-Path (Join-Path $Dir 'package.json')) {
    Say "Aether already lives at $Dir - refreshing it."
    git -C $Dir pull --ff-only
    if ($LASTEXITCODE -ne 0) { Note "Couldn't pull; keeping the existing copy" }
    return
  }
  if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail 'git is needed to fetch Aether' }
  Say "Fetching Aether into $Dir ..."
  New-Item -ItemType Directory -Force -Path (Split-Path $Dir) | Out-Null
  git clone --depth 1 $AetherRepo $Dir
  if ($LASTEXITCODE -ne 0) { Fail "Clone failed: $AetherRepo" }
}

function Install-Deps {
  Say 'Installing dependencies (grab a coffee - first run takes a few minutes)...'
  Push-Location $Dir
  try {
    pnpm install
    if ($LASTEXITCODE -ne 0) { Fail 'pnpm install failed' }
  } finally { Pop-Location }
}

# --- main -------------------------------------------------------------------
if ($NodeOnly) {
  if (-not (Test-NodeReady)) { Install-Node $NodePrefix }
  exit 0
}

Say 'Welcome to Aether - Windows edition'
Require-Node
Require-Pnpm
Get-Aether
Install-Deps

Write-Host ""
Write-Host "  Aether lives at: $Dir"
Write-Host ""
Write-Host '  Start it whenever you like:'
Write-Host "    cd `"$Dir`" ; pnpm start"
Write-Host ""

if (-not $NoSetup) {
  Say 'Handing you over to first-run setup...'
  Set-Location $Dir
  node scripts/run-app.mjs ignite
  exit $LASTEXITCODE
} else {
  Say "First-run skipped (-NoSetup). Whenever you're ready: node scripts/run-app.mjs ignite"
}
