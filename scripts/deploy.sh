#!/usr/bin/env bash
#
# Aether deploy engine (Linux) - the Render/UptimeRobot step behind the wizard.
#
# The interactive setup is scripts/setup.sh, which runs the keyboard-driven
# wizard (src/aether/setup) and writes its answers to
# ~/.aether-setup/answers.env and its secrets to ~/.aether-setup/secrets.env.
# This engine takes those files and does the deployment non-interactively:
#
#   ./scripts/deploy.sh --answers ~/.aether-setup/answers.env
#
# It reads the secrets from ~/.aether-setup/secrets.env automatically. Run it
# without --answers only if you want the legacy terminal prompts; the wizard is
# the supported way to answer the questions.
#
# What it does: generates the config and any missing secrets, creates or
# updates a Render web service, waits for the first deploy, verifies /healthz
# and the panel, creates an UptimeRobot monitor so the free instance does not
# spin down, and prints a masked summary.
#
# A machine that already has setup state is left alone; pass --force to run
# again. A forced run finds the existing Render service and monitor and updates
# them instead of creating duplicates. It never deletes a Render service or a
# monitor.
#
# Requirements on this machine: bash, coreutils (base64, head, date, chmod) and
# curl. No Python, jq or Node needed.
#
# Usage:
#   ./scripts/setup.sh [--dry-run] [--answers FILE] [--secrets FILE]
#                      [--non-interactive] [--help]
#
# See docs/hosted-setup.md for the shared answers-file format and the full
# question list. scripts/setup.bat asks the same questions in the same order and
# reads the same answers file.

set -u

SCRIPT_PATH=$(readlink -f "$0" 2>/dev/null || echo "$0")
SCRIPT_DIR=$(cd "$(dirname "$SCRIPT_PATH")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)

RENDER_API="${AETHER_RENDER_API:-https://api.render.com/v1}"
UPTIMEROBOT_API="https://api.uptimerobot.com/v2"
UPTIMEROBOT_AGENTIC="https://api.uptimerobot.com/agentic/agent-monitor"

STATE_DIR="${AETHER_SETUP_DIR:-$HOME/.aether-setup}"
STATE_FILE="$STATE_DIR/state.env"
SECRETS_FILE="$STATE_DIR/secrets.env"
ANSWERS_FILE=""
SECRETS_INPUT=""
DRY_RUN=0
NON_INTERACTIVE=0
FORCE=0

# Answers, filled by the questions or the answers file.
DATABASE_MODE=""
DATABASE_URL=""
LLM_PROVIDER=""
LLM_MODEL=""
SERVICES=""
APPS=""
CHANNELS=""
PLAN=""
SERVICE_NAME=""
REGION=""
DEPLOY_SOURCE=""
REPO_URL=""
UPTIME_MODE=""
ALERT_EMAIL=""
LOCAL_COPY=""
WORKBENCH_PROVIDER=""
IMAGE_PROVIDER=""

# Secrets, kept in memory only (plus an owner-only state file for resume).
# An exported value wins over an empty default; files loaded below win over
# both, because a saved resume file is the most deliberate source.
: "${AETHER_ENCRYPTION_KEY:=}"
: "${AETHER_TOKEN:=}"
: "${TOKEN_ENC_KEY:=}"
: "${TOKEN_ENC_KEY_ID:=}"
: "${RENDER_API_KEY:=}"
: "${TAVILY_KEY:=}"
: "${GROQ_KEY:=}"
: "${GEMINI_KEY:=}"
: "${ANTHROPIC_KEY:=}"
: "${OPENAI_KEY:=}"
: "${NVIDIA_KEY:=}"
: "${TELEGRAM_TOKEN:=}"
: "${DISCORD_TOKEN:=}"
: "${GOOGLE_CLIENT_ID:=}"
: "${GOOGLE_CLIENT_SECRET:=}"
: "${GOOGLE_PICKER_KEY:=}"
: "${GOOGLE_PICKER_APP_ID:=}"
: "${GITHUB_CLIENT_ID:=}"
: "${GITHUB_CLIENT_SECRET:=}"
: "${LINKEDIN_CLIENT_ID:=}"
: "${LINKEDIN_CLIENT_SECRET:=}"
: "${MODAL_TOKEN_ID:=}"
: "${MODAL_TOKEN_SECRET:=}"
: "${VERCEL_TOKEN:=}"
: "${UPTIME_API_KEY:=}"
# whether this run had to make a new content key — the update path uses it
# to tell "the operator supplied this key" from "the resume file was gone"
AETHER_ENCRYPTION_KEY_GENERATED=0
ROTATE_ENCRYPTION_KEY=0

# Results.
SERVICE_ID=""
SERVICE_URL=""
DEPLOY_ID=""
MONITOR_ID=""
CONFIG_YAML=""
CONFIG_B64=""

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

hr() { printf '%s\n' "------------------------------------------------------------"; }

say()  { printf '%s\n' "$*"; }
ok()   { printf '[ ok ] %s\n' "$*"; }
warn() { printf '[warn] %s\n' "$*"; }
die()  { printf '[stop] %s\n' "$*" >&2; exit 1; }

mask() {
  # Mask a secret, showing only enough to recognise it. Never logs the value.
  local v="$1"
  local n=${#v}
  if [ "$n" -eq 0 ]; then printf '(empty)'; return; fi
  if [ "$n" -le 8 ]; then printf '********'; return; fi
  printf '%s...%s' "$(printf '%s' "$v" | cut -c1-4)" "$(printf '%s' "$v" | rev | cut -c1-4 | rev)"
}

urlencode() {
  # Minimal percent-encoding for query values (curl --data-urlencode is used
  # where possible; this is for URLs printed to the user).
  local s="$1"
  local out="" c
  local i=0
  while [ "$i" -lt "${#s}" ]; do
    c="${s:$i:1}"
    case "$c" in
      [a-zA-Z0-9.~_-]) out="$out$c" ;;
      *) out="$out$(printf '%%%02X' "'$c")" ;;
    esac
    i=$((i + 1))
  done
  printf '%s' "$out"
}

open_url() {
  # Open a URL in the user's browser, falling back to printing it.
  #
  # The launcher is detached on purpose. xdg-open and gio open run the browser
  # in the foreground, and the browser inherits this script's stdin. When the
  # browser does not fork (or there is no running instance to hand off to),
  # open_url blocks until the browser exits and the terminal's keyboard input
  # goes to the browser instead of this script, which looks like a frozen
  # script that ignores what you type. Backgrounding the launcher and giving it
  # /dev/null for stdin keeps the script's own reads working.
  local url="$1"
  if [ "$DRY_RUN" -eq 1 ]; then
    say "  [dry-run] would open: $url"
    return 0
  fi
  if [ "$NON_INTERACTIVE" -eq 1 ]; then
    say "  $url"
    return 0
  fi
  say "  Opening: $url"
  if command -v xdg-open >/dev/null 2>&1; then
    ( xdg-open "$url" >/dev/null 2>&1 ) </dev/null &
    disown 2>/dev/null || true
  elif command -v gio >/dev/null 2>&1; then
    ( gio open "$url" >/dev/null 2>&1 ) </dev/null &
    disown 2>/dev/null || true
  else
    warn "no xdg-open or gio found; open this link yourself:"
    say "  $url"
  fi
}

json_escape() {
  # Escape a string for embedding inside a JSON double-quoted value.
  # The only characters that matter for our values are backslash and quote.
  local s="$1"
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  printf '%s' "$s"
}

json_field() {
  # Print the first string value for a key, tolerating whitespace around ':'
  # and newlines in the response.
  local key="$1" body="$2"
  printf '%s' "$body" | tr -d '\n' \
    | grep -oE "\"$key\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" | head -n 1 \
    | sed -E "s/\"$key\"[[:space:]]*:[[:space:]]*\"//; s/\"$//"
}

json_id() {
  # json_id srv "$body" -> the first srv-... id. Reads stdin when no body arg.
  local prefix="$1" body="${2:-}"
  [ -n "$body" ] || body=$(cat)
  printf '%s' "$body" | tr -d '\n' \
    | grep -oE "\"id\"[[:space:]]*:[[:space:]]*\"$prefix-[^\"]*\"" | head -n 1 \
    | sed -E "s/.*\"($prefix-[^\"]*)\"/\1/"
}

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

usage() {
  cat <<'USAGE'
Aether deploy engine (Linux)

Usage:
  ./scripts/deploy.sh [options]

Run the interactive setup first (scripts/setup.sh). It writes the answers to
~/.aether-setup/answers.env and the secrets to ~/.aether-setup/secrets.env.
Then deploy with:

  ./scripts/deploy.sh --answers ~/.aether-setup/answers.env

Options:
  --dry-run            Ask the questions, show every action, change nothing.
  --answers FILE       Read answers from FILE (KEY=value lines). Implies
                       --non-interactive.
  --secrets FILE       Read secrets from FILE (KEY=value, chmod 600).
  --set KEY=VALUE      Set one answer or secret (repeatable). Implies
                       --non-interactive.
  --non-interactive    Never prompt; use answers, secrets, and defaults.
  --force              Run even when this machine already has setup state.
                       Without it, a second run exits without changing anything.
  --state-dir DIR      Where to keep resume state (default ~/.aether-setup).
  --rotate-encryption-key
                       Replace the deployed content key on an existing
                       service. This makes every previously stored chat,
                       memory, and trace row unreadable. Off by default.
                       Requires --force on a machine that already ran setup.
  -h, --help           Show this help.

Environment:
  AETHER_SETUP_DIR     Same as --state-dir.
  RENDER_API_KEY, UPTIME_API_KEY, and the Aether secrets can also be passed
  through the environment for unattended runs.

The Windows engine scripts/deploy.bat takes the same options and reads the same
answers file. The interactive wizard is scripts/setup.sh / setup.bat.
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --non-interactive) NON_INTERACTIVE=1 ;;
    --force) FORCE=1 ;;
    --answers) shift; ANSWERS_FILE="${1:-}" ;;
    --secrets) shift; SECRETS_INPUT="${1:-}" ;;
    --set)
      shift
      _kv="${1:-}"
      _k="${_kv%%=*}"
      _v="${_kv#*=}"
      case "$_k" in
        [A-Za-z_]*)
          case "$_k" in *[!A-Za-z0-9_]*) ;; *) printf -v "$_k" '%s' "$_v" ;; esac ;;
      esac
      NON_INTERACTIVE=1
      ;;
    --state-dir) shift; STATE_DIR="${1:-}"; STATE_FILE="$STATE_DIR/state.env"; SECRETS_FILE="$STATE_DIR/secrets.env" ;;
    --rotate-encryption-key) ROTATE_ENCRYPTION_KEY=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

if [ -n "$ANSWERS_FILE" ]; then NON_INTERACTIVE=1; fi

# A machine that already has setup state is not set up again by accident.
# A dry-run is allowed because it changes nothing; everything else needs the
# explicit --force.
if [ -s "$STATE_FILE" ] && [ "$FORCE" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
  say "Aether setup has already run on this machine."
  say "  state: $STATE_FILE"
  say "Nothing was changed. Re-run with --force to update the deployment."
  exit 0
fi

# ---------------------------------------------------------------------------
# Repo discovery and the repo's own catalogs (nothing is hardcoded)
# ---------------------------------------------------------------------------

[ -f "$REPO_ROOT/pyproject.toml" ] || die "run this from inside the Aether repo (pyproject.toml not found above $SCRIPT_DIR)"
[ -f "$REPO_ROOT/config.example.yaml" ] || die "config.example.yaml not found; is this the Aether repo?"
[ -f "$REPO_ROOT/src/aether/config.py" ] || die "src/aether/config.py not found; is this the Aether repo?"

# Check and install the deploy requirements (curl, base64) before touching the
# network. Python is optional here; it only powers key generation and the
# UptimeRobot email flow, and both have fallbacks.
DEPLOY_PYTHON="${PYTHON:-python3}"
if command -v "$DEPLOY_PYTHON" >/dev/null 2>&1; then
  PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
    "$DEPLOY_PYTHON" -m aether.setup.requirements --mode deploy || die "deploy requirements are not satisfied"
else
  for _cmd in curl base64; do
    command -v "$_cmd" >/dev/null 2>&1 || die "'$_cmd' is required to deploy; install it and re-run"
  done
fi

# Model providers, read from the pydantic Literal in config.py.
discover_providers() {
  grep -o 'provider: Literal\[[^]]*\]' "$REPO_ROOT/src/aether/config.py" \
    | head -n 1 | sed 's/.*\[//;s/\]//' | tr -d '"' | tr ',' ' ' | tr -s ' '
}

# The example model for a provider, read from config.example.yaml comments.
discover_example_model() {
  local provider="$1"
  local from_comments
  from_comments=$(grep -o "$provider,[^#]*model: [A-Za-z0-9._/-]*" "$REPO_ROOT/config.example.yaml" \
    | head -n 1 | sed 's/.*model: //')
  if [ -n "$from_comments" ]; then printf '%s' "$from_comments"; return; fi
  # Fall back to the top-level default model.
  grep -m1 '^  model:' "$REPO_ROOT/config.example.yaml" | sed 's/.*model:[[:space:]]*//'
}

# Default main model from config.example.yaml.
discover_default_model() {
  grep -m1 '^  model:' "$REPO_ROOT/config.example.yaml" | sed 's/.*model:[[:space:]]*//'
}

# Direct apps, read from the AppDefinition blocks in the provider modules.
discover_direct_apps() {
  local f
  for f in "$REPO_ROOT"/src/aether/providers/*.py; do
    [ -f "$f" ] || continue
    awk '
      / = AppDefinition\(/ { inapp=1; k=""; n=""; next }
      inapp && /^[[:space:]]*key=/ && k=="" { k=$0; sub(/.*key="/,"",k); sub(/".*/,"",k) }
      inapp && /^[[:space:]]*name=/ && n=="" { n=$0; sub(/.*name="/,"",n); sub(/".*/,"",n) }
      inapp && /^\)/ { if (k!="") print k "|" n; inapp=0 }
    ' "$f"
  done | sort -u
}

# Chat channels, read from the messaging recipes.
discover_channels() {
  awk '
    /key="/ { k=$0; sub(/.*key="/,"",k); sub(/".*/,"",k) }
    /kind="messaging"/ { if (k!="") { print k; k="" } }
  ' "$REPO_ROOT/src/aether/apps_catalog.py" | sort -u
}

# Optional services, read from the quickstart's Optional services table.
discover_optional_services() {
  awk '
    /^## Optional services/ { f=1; next }
    /^## / { if (f) exit }
    f && /^\|/ {
      line=$0
      sub(/^\|[[:space:]]*/,"",line)
      sub(/[[:space:]]*\|.*/,"",line)
      if (line!="" && line!="Capability" && line !~ /^-+$/) print line
    }
  ' "$REPO_ROOT/docs/quickstart.md"
}

# Cloud workbench providers, read from config.example.yaml.
discover_workbench_providers() {
  grep -m1 'provider: modal' "$REPO_ROOT/config.example.yaml" \
    | sed 's/.*#[[:space:]]*//' | tr '|' ' ' | tr -s ' '
}

PROVIDERS=$(discover_providers)
[ -n "$PROVIDERS" ] || die "could not read the provider list from src/aether/config.py"
DEFAULT_MODEL=$(discover_default_model)
[ -n "$DEFAULT_MODEL" ] || DEFAULT_MODEL="claude-opus-5"
WORKBENCH_PROVIDERS=$(discover_workbench_providers)
[ -n "$WORKBENCH_PROVIDERS" ] || WORKBENCH_PROVIDERS="modal vercel github_actions none"

DIRECT_APPS=$(discover_direct_apps)
CHANNELS_LIST=$(discover_channels)
OPTIONAL_SERVICES=$(discover_optional_services)

# ---------------------------------------------------------------------------
# Resume state and owner-only secrets
# ---------------------------------------------------------------------------

mkdir -p "$STATE_DIR" 2>/dev/null || true

load_kv() {
  # Read KEY=value lines into the shell, without sourcing (no code execution).
  local file="$1"
  [ -f "$file" ] || return 0
  local line key value
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line#$'\xEF\xBB\xBF'}"
    case "$line" in \#*|"") continue ;; esac
    key="${line%%=*}"
    value="${line#*=}"
    value="${value%$'\r'}"
    case "$key" in
      *[!A-Za-z0-9_]*) continue ;;
      [0-9]*) continue ;;
    esac
    printf -v "$key" '%s' "$value" 2>/dev/null || true
  done < "$file"
}

if [ -f "$STATE_FILE" ]; then load_kv "$STATE_FILE"; fi
if [ -f "$SECRETS_FILE" ]; then load_kv "$SECRETS_FILE"; fi
if [ -n "$SECRETS_INPUT" ] && [ -f "$SECRETS_INPUT" ]; then load_kv "$SECRETS_INPUT"; fi
if [ -n "$ANSWERS_FILE" ] && [ -f "$ANSWERS_FILE" ]; then load_kv "$ANSWERS_FILE"; fi

save_secret() {
  # Append or update one KEY=value in the owner-only secrets file.
  local key="$1" value="$2"
  [ -n "$value" ] || return 0
  if [ "$DRY_RUN" -eq 1 ]; then return 0; fi
  touch "$SECRETS_FILE"
  chmod 600 "$SECRETS_FILE" 2>/dev/null || true
  local tmp
  tmp="$SECRETS_FILE.tmp"
  grep -v "^$key=" "$SECRETS_FILE" > "$tmp" 2>/dev/null || true
  printf '%s=%s\n' "$key" "$value" >> "$tmp"
  mv "$tmp" "$SECRETS_FILE"
  chmod 600 "$SECRETS_FILE" 2>/dev/null || true
}

save_state() {
  local key="$1" value="$2"
  [ -n "$value" ] || return 0
  if [ "$DRY_RUN" -eq 1 ]; then return 0; fi
  touch "$STATE_FILE"
  local tmp
  tmp="$STATE_FILE.tmp"
  grep -v "^$key=" "$STATE_FILE" > "$tmp" 2>/dev/null || true
  printf '%s=%s\n' "$key" "$value" >> "$tmp"
  mv "$tmp" "$STATE_FILE"
}

# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------

ask_line() {
  # ask_line VARNAME "Question" "default"
  local var="$1" question="$2" default="${3:-}" current answer
  eval current="\${$var:-}"
  if [ -n "$current" ]; then return 0; fi
  if [ "$NON_INTERACTIVE" -eq 1 ]; then
    printf -v "$var" '%s' "$default"
    return 0
  fi
  if [ -n "$default" ]; then
    printf '%s [%s]: ' "$question" "$default"
  else
    printf '%s: ' "$question"
  fi
  IFS= read -r answer || true
  [ -n "$answer" ] || answer="$default"
  printf -v "$var" '%s' "$answer"
}

ask_secret() {
  # ask_secret VARNAME "Question" - reads without echoing, keeps in memory.
  local var="$1" question="$2" current answer
  eval current="\${$var:-}"
  if [ -n "$current" ]; then
    ok "already set: $(mask "$current")"
    return 0
  fi
  if [ "$NON_INTERACTIVE" -eq 1 ]; then
    eval answer="\${$var:-}"
    if [ -z "$answer" ]; then
      warn "no value supplied for $var (non-interactive)"
    fi
    return 0
  fi
  printf '%s: ' "$question"
  stty -echo 2>/dev/null || true
  IFS= read -r answer || true
  stty echo 2>/dev/null || true
  printf '\n'
  printf -v "$var" '%s' "$answer"
}

menu_single() {
  # menu_single VARNAME "Title" default_index option1 "option2" ...
  local var="$1" title="$2" default="$3"
  shift 3
  local options=("$@")
  local current i choice
  eval current="\${$var:-}"
  if [ -n "$current" ]; then
    # Validate against the option labels used as values.
    for i in "${!options[@]}"; do
      if [ "${options[$i]}" = "$current" ]; then return 0; fi
    done
    # Accept a 1-based index in the answers file too.
    case "$current" in
      ''|*[!0-9]*) ;;
      *) if [ "$current" -ge 1 ] && [ "$current" -le "${#options[@]}" ]; then
           printf -v "$var" '%s' "${options[$((current - 1))]}"; return 0
         fi ;;
    esac
    return 0
  fi
  if [ "$NON_INTERACTIVE" -eq 1 ]; then
    printf -v "$var" '%s' "${options[$((default - 1))]}"
    return 0
  fi
  say ""
  say "$title"
  for i in "${!options[@]}"; do
    if [ "$((i + 1))" -eq "$default" ]; then
      printf '  %d) %s (default)\n' "$((i + 1))" "${options[$i]}"
    else
      printf '  %d) %s\n' "$((i + 1))" "${options[$i]}"
    fi
  done
  printf 'Choose [%d]: ' "$default"
  IFS= read -r choice || true
  [ -n "$choice" ] || choice="$default"
  case "$choice" in
    ''|*[!0-9]*) choice="$default" ;;
  esac
  if [ "$choice" -lt 1 ] || [ "$choice" -gt "${#options[@]}" ]; then choice="$default"; fi
  printf -v "$var" '%s' "${options[$((choice - 1))]}"
}

menu_multi() {
  # menu_multi VARNAME "Title" "default_csv(1-based)" option1 "option2" ...
  local var="$1" title="$2" default="$3"
  shift 3
  local options=("$@")
  local current i choice item
  eval current="\${$var:-}"
  if [ -n "$current" ]; then return 0; fi
  if [ "$NON_INTERACTIVE" -eq 1 ]; then
    printf -v "$var" '%s' "$default"
    return 0
  fi
  say ""
  say "$title"
  say "  (comma-separated numbers, or Enter for the default)"
  for i in "${!options[@]}"; do
    local mark=" "
    case ",$default," in *",$((i + 1)),"*) mark="x" ;; esac
    printf '  %d) [%s] %s\n' "$((i + 1))" "$mark" "${options[$i]}"
  done
  printf 'Choose [%s]: ' "$default"
  IFS= read -r choice || true
  [ -n "$choice" ] || choice="$default"
  printf -v "$var" '%s' "$choice"
}

csv_has() {
  # csv_has "1,3" "3" -> true
  case ",$1," in *",$2,"*) return 0 ;; *) return 1 ;; esac
}

index_of() {
  # index_of "value" option1 option2 ... -> 1-based index, or nothing
  local needle="$1"; shift
  local n=1 item
  for item in "$@"; do
    if [ "$item" = "$needle" ]; then printf '%s' "$n"; return 0; fi
    n=$((n + 1))
  done
  return 1
}

# ---------------------------------------------------------------------------
# The questions (same order as setup.bat and docs/hosted-setup.md)
# ---------------------------------------------------------------------------

discover_upstream_repo() {
  git -C "$REPO_ROOT" config --get remote.origin.url 2>/dev/null \
    | sed 's#^git@github.com:#https://github.com/#' \
    | sed 's#\.git$##'
}

UPSTREAM_REPO=$(discover_upstream_repo)
[ -n "$UPSTREAM_REPO" ] || UPSTREAM_REPO=$(grep -o 'https://github.com/[^ ]*/Aether[^ )]*' "$REPO_ROOT/docs/quickstart.md" | head -n 1)
[ -n "$UPSTREAM_REPO" ] || UPSTREAM_REPO="https://github.com/Vedant-Git-dev/Aether"

map_indices() {
  # map_indices "1,3" option1 option2 option3 -> "option1,option3"
  local csv="$1"; shift
  local opts=("$@")
  local out="" idx
  local IFS=','
  for idx in $csv; do
    case "$idx" in
      ''|' '*) continue ;;
      *[!0-9]*)
        # Already a name, keep it (answers files may use names).
        [ -n "$out" ] && out="$out,"
        out="$out$idx"
        continue ;;
    esac
    if [ "$idx" -ge 1 ] && [ "$idx" -le "${#opts[@]}" ]; then
      [ -n "$out" ] && out="$out,"
      out="$out${opts[$((idx - 1))]}"
    fi
  done
  printf '%s' "$out"
}

ask_questions() {
  say ""
  hr
  say "Aether hosted setup"
  hr
  say "Nothing secret is written to the repo. Re-running with --force updates"
  say "instead of duplicating. Ctrl+C at any point stops cleanly."
  say ""

  # 1. Database
  menu_single DATABASE_MODE "1. Database" 1 \
    "neon" \
    "existing" \
    "render"
  case "$DATABASE_MODE" in
    neon) say "  Neon free tier: 0.5 GB, 100 compute-hours/month, does not expire." ;;
    render) warn "Render's free Postgres expires 30 days after creation (14-day grace), 1 GB, no backups. Neon is recommended." ;;
    existing) say "  You will paste a Postgres connection string (keep sslmode=require)." ;;
  esac

  # 2. Model provider and main model
  local provider_opts=() p
  for p in $PROVIDERS; do provider_opts+=("$p"); done
  local provider_default=1
  p=$(index_of "anthropic" "${provider_opts[@]}") && provider_default="$p"
  menu_single LLM_PROVIDER "2. Main model provider" "$provider_default" "${provider_opts[@]}"
  local example_model
  example_model=$(discover_example_model "$LLM_PROVIDER")
  [ -n "$example_model" ] || example_model="$DEFAULT_MODEL"
  say ""
  say "  Model naming (from docs/models.md): a bare id uses llm.provider;"
  say "  provider/model routes to that provider (for example openai/gpt-5.1)."
  say "  Sentinel words off/default are meanings, not model ids."
  ask_line LLM_MODEL "  Main model" "$example_model"

  # 3. Optional services (multi-select); screen capture is Linux-only and omitted.
  local service_opts=() svc
  while IFS= read -r svc; do
    case "$svc" in
      ""|"Capability") continue ;;
    esac
    case "$(printf '%s' "$svc" | tr '[:upper:]' '[:lower:]')" in
      *"screen capture"*) continue ;;
      *"semantic memory"*) continue ;;
    esac
    service_opts+=("$svc")
  done <<SERVICES_LIST
$OPTIONAL_SERVICES
SERVICES_LIST
  if [ "${#service_opts[@]}" -gt 0 ]; then
    menu_multi SERVICES "3. Optional services (multi-select)" "" "${service_opts[@]}"
    SERVICES=$(map_indices "$SERVICES" "${service_opts[@]}")
  fi
  say "  Semantic memory is always on (Gemini embeddings); it is not an optional choice."
  case "$(printf '%s' ",$SERVICES," | tr '[:upper:]' '[:lower:]')" in
    *"image generation"*) ask_line IMAGE_PROVIDER "  Image provider (pollinations|gemini|none)" "pollinations" ;;
    *) IMAGE_PROVIDER="none" ;;
  esac
  case "$(printf '%s' ",$SERVICES," | tr '[:upper:]' '[:lower:]')" in
    *"background jobs"*) ask_line WORKBENCH_PROVIDER "  Background-work sandbox provider ($WORKBENCH_PROVIDERS)" "modal" ;;
    *) WORKBENCH_PROVIDER="none" ;;
  esac

  # 4. Direct apps (multi-select)
  local app_keys=() app_line
  while IFS= read -r app_line; do
    [ -n "$app_line" ] || continue
    app_keys+=("${app_line%%|*}")
  done <<APPS_LIST
$DIRECT_APPS
APPS_LIST
  if [ "${#app_keys[@]}" -gt 0 ]; then
    say ""
    say "  Direct apps use a sign-in you own. They need the token-encryption"
    say "  keyring, which this script generates for you."
    menu_multi APPS "4. Direct apps (multi-select)" "" "${app_keys[@]}"
    APPS=$(map_indices "$APPS" "${app_keys[@]}")
  fi

  # 5. Chat channels (multi-select)
  local channel_opts=() ch
  while IFS= read -r ch; do [ -n "$ch" ] && channel_opts+=("$ch"); done <<CHANNEL_LIST
$CHANNELS_LIST
CHANNEL_LIST
  if [ "${#channel_opts[@]}" -gt 0 ]; then
    menu_multi CHANNELS "5. Chat channels (multi-select)" "" "${channel_opts[@]}"
    CHANNELS=$(map_indices "$CHANNELS" "${channel_opts[@]}")
  fi

  # 6. Render plan
  say ""
  say "  Plan trade-offs:"
  say "    free    - 0 USD: 512 MB, 0.1 CPU, spins down after 15 idle minutes,"
  say "              ephemeral disk (the workspace resets on restart),"
  say "              750 instance-hours per workspace per month."
  say "    starter - about 7 USD/month: 512 MB, 0.5 CPU, no spin-down, and a"
  say "              1 GB persistent disk (\$0.25/GB/month) keeps the workspace."
  menu_single PLAN "6. Render plan" 1 \
    "free" \
    "starter"
  case "$PLAN" in
    free) say "  Free: 512 MB RAM, 0.1 CPU, spins down after 15 idle minutes (a monitor keeps it awake), ephemeral disk, 750 instance-hours/workspace/month." ;;
    starter) say "  Starter: about 7 USD/month, 512 MB RAM, 0.5 CPU, no spin-down, can attach a persistent disk so the workspace survives restarts." ;;
  esac

  # 7. Render details
  ask_line SERVICE_NAME "7a. Render service name" "aether"
  if [ "$NON_INTERACTIVE" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
    while :; do
      _code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "https://$SERVICE_NAME.onrender.com" 2>/dev/null || echo 000)
      case "$_code" in
        200|301|302|403|503)
          warn "https://$SERVICE_NAME.onrender.com already answers (HTTP $_code)."
          say "  Render would append a suffix and need one extra redeploy."
          printf '  Service name [%s]: ' "$SERVICE_NAME"
          IFS= read -r _newname || true
          [ -n "$_newname" ] || break
          SERVICE_NAME="$_newname"
          ;;
        *) break ;;
      esac
    done
  fi
  menu_single REGION "7b. Render region" 1 oregon ohio virginia frankfurt singapore
  say ""
  say "  7c. Deploy source"
  say "    public - deploy the public upstream repo (fastest; no GitHub link needed,"
  say "             but config.yaml defaults apply unless you add one via Render)"
  say "    fork   - deploy your own fork (required for full API automation and commits)"
  menu_single DEPLOY_SOURCE "  Deploy source" 1 "public" "fork"
  if [ "$DEPLOY_SOURCE" = "fork" ]; then
    ask_line REPO_URL "  Your fork URL" "https://github.com/CHANGE_ME/Aether"
    say "  When Render asks, authorize the GitHub app so it can see your fork."
    say "  Dashboard: New, Blueprint, connect GitHub, pick the fork."
  else
    REPO_URL="$UPSTREAM_REPO"
  fi
  REPO_URL="${REPO_URL%.git}"
  REPO_URL=$(printf '%s' "$REPO_URL" | sed 's#^git@github.com:#https://github.com/#')

  # 8. Uptime monitoring
  say ""
  say "  8. Uptime monitoring (keeps the free instance awake and alerts you)"
  say "    api     - create a keyword monitor with your UptimeRobot API key"
  say "    email   - no API key: UptimeRobot emails you a one-click confirm link"
  say "    manual  - print the exact steps and open the page"
  say "    skip    - do nothing"
  menu_single UPTIME_MODE "  Uptime monitoring" 1 "api" "email" "manual" "skip"
  if [ "$UPTIME_MODE" = "api" ] || [ "$UPTIME_MODE" = "email" ]; then
    ask_line ALERT_EMAIL "  Alert email address" ""
  fi

  # 9. Local copy
  menu_single LOCAL_COPY "9. Also set up a local copy for testing?" 1 "no" "yes"

  say ""
  hr
  say "Choices"
  hr
  say "  database:        $DATABASE_MODE"
  say "  model:           $LLM_PROVIDER / $LLM_MODEL"
  say "  services:        ${SERVICES:-(none)}"
  say "  direct apps:     ${APPS:-(none)}"
  say "  channels:        ${CHANNELS:-(none)}"
  say "  plan:            $PLAN"
  say "  service name:    $SERVICE_NAME"
  say "  region:          $REGION"
  say "  deploy source:   $DEPLOY_SOURCE ($REPO_URL)"
  say "  uptime:          $UPTIME_MODE"
  say "  local copy:      $LOCAL_COPY"
  if [ "$NON_INTERACTIVE" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
    local go
    printf 'Proceed? [Y/n]: '
    IFS= read -r go || true
    case "$go" in
      [Nn]*) die "stopped at your request; nothing was changed" ;;
    esac
  fi
}

# ---------------------------------------------------------------------------
# Credentials and generated secrets
# ---------------------------------------------------------------------------

gen_key_b64() {
  # 32 random bytes, base64. Matches the quickstart's generate_key_b64().
  if command -v python3 >/dev/null 2>&1 && python3 -c "import aether.memory.crypto" >/dev/null 2>&1; then
    python3 -c "from aether.memory.crypto import generate_key_b64; print(generate_key_b64())"
  else
    head -c 32 /dev/urandom | base64 | tr -d '\n'
  fi
}

gen_token() {
  head -c 48 /dev/urandom | base64 | tr -d '\n=/' | cut -c1-64
}

b64_nowrap() {
  if base64 --help 2>&1 | grep -q -- '-w'; then
    base64 -w 0
  else
    base64 | tr -d '\n'
  fi
}

has_service() {
  case "$(printf '%s' ",$SERVICES," | tr '[:upper:]' '[:lower:]')" in *"$1"*) return 0 ;; *) return 1 ;; esac
}

gather_credentials() {
  say ""
  hr
  say "Credentials"
  hr
  say "Pasted values are read without echo, never printed, and stored only in"
  say "Render (and in an owner-only file under $STATE_DIR for resume)."
  say ""

  # --- Render account and API key ---------------------------------------
  # Only one browser tab is opened. The API key is what the next prompt needs,
  # so the API-keys page is opened and the sign-up link is printed instead of
  # also opening it (two Render tabs in a row was noise).
  say "A Render account is required. If you do not have one, create it first:"
  say "  https://dashboard.render.com/register"
  say "  Render dashboard: https://dashboard.render.com"
  say ""
  say "A Render API key lets this script create and configure everything."
  say "Without one, the script writes a Blueprint and walks you through the"
  say "dashboard instead."
  open_url "https://dashboard.render.com/u/settings#api-keys"
  say "  Render Dashboard, top right, Account Settings, API Keys, Create API Key."
  ask_secret RENDER_API_KEY "Render API key (or press Enter to skip and use the dashboard)"
  if [ -n "$RENDER_API_KEY" ]; then
    say "  Render API key set: $(mask "$RENDER_API_KEY")"
  else
    say "  No Render API key: the dashboard walkthrough will be used."
  fi

  # --- Database ----------------------------------------------------------
  case "$DATABASE_MODE" in
    neon)
      say ""
      say "Neon free Postgres (recommended). Steps:"
      say "  1. Create a project at https://console.neon.tech"
      say "  2. Open the project, copy the connection string"
      say "  3. Keep sslmode=require"
      open_url "https://console.neon.tech"
      ask_secret DATABASE_URL "Paste the Neon connection string (postgresql://...?sslmode=require)"
      ;;
    existing)
      ask_secret DATABASE_URL "Paste your Postgres connection string (keep sslmode=require)"
      ;;
    render)
      warn "Render's free Postgres expires 30 days after creation; Neon is recommended."
      say "Create it in the dashboard: New, Postgres. Then copy its External"
      say "Connection String here."
      open_url "https://dashboard.render.com/new/database"
      ask_secret DATABASE_URL "Paste the Render Postgres external connection string"
      ;;
  esac
  [ -n "$DATABASE_URL" ] && save_secret DATABASE_URL "$DATABASE_URL"

  # --- Model provider key ------------------------------------------------
  say ""
  case "$LLM_PROVIDER" in
    anthropic)
      say "Anthropic API key: create one in the Anthropic Console."
      open_url "https://console.anthropic.com/settings/keys"
      ask_secret ANTHROPIC_KEY "Anthropic API key" ;;
    openai)
      say "OpenAI API key: create one in the OpenAI dashboard."
      open_url "https://platform.openai.com/api-keys"
      ask_secret OPENAI_KEY "OpenAI API key" ;;
    gemini)
      say "Google Gemini API key: create one in Google AI Studio."
      open_url "https://aistudio.google.com/apikey"
      ask_secret GEMINI_KEY "Gemini API key" ;;
    nvidia)
      say "NVIDIA NIM API key: create one on build.nvidia.com."
      open_url "https://build.nvidia.com/"
      ask_secret NVIDIA_KEY "NVIDIA API key" ;;
    ollama)
      warn "Ollama runs on your machine only; it cannot run on Render. Aether will boot but the main model will be unavailable." ;;
  esac

  # --- Optional services -------------------------------------------------
  if has_service "web search"; then
    say ""
    say "Web search uses Tavily's free tier."
    open_url "https://app.tavily.com"
    ask_secret TAVILY_KEY "Tavily API key"
  fi
  if has_service "voice-note transcription"; then
    say ""
    say "Voice-note transcription uses Groq (whisper-large-v3)."
    open_url "https://console.groq.com/keys"
    ask_secret GROQ_KEY "Groq API key"
  fi
  if [ -z "$GEMINI_KEY" ]; then
    say ""
    say "Semantic memory is always on and uses Gemini embeddings."
    say "A Gemini API key is required for it."
    open_url "https://aistudio.google.com/apikey"
    ask_secret GEMINI_KEY "Gemini API key (semantic memory)"
  fi
  if has_service "background jobs"; then
    say ""
    say "Background jobs run in a cloud sandbox ($WORKBENCH_PROVIDER)."
    case "$WORKBENCH_PROVIDER" in
      modal)
        say "  Modal: create a token pair in the Modal dashboard."
        open_url "https://modal.com/settings/tokens"
        ask_secret MODAL_TOKEN_ID "Modal token ID"
        ask_secret MODAL_TOKEN_SECRET "Modal token secret" ;;
      vercel)
        say "  Vercel: create a token at https://vercel.com/account/tokens and note the team and project ids."
        open_url "https://vercel.com/account/tokens"
        ask_secret VERCEL_TOKEN "Vercel token" ;;
      github_actions)
        say "  GitHub Actions batch queue: a token with repo and workflow scope."
        open_url "https://github.com/settings/tokens"
        ask_secret GITHUB_CLIENT_ID "GitHub Actions token (stored as GITHUB_ACTIONS_TOKEN placeholder)" ;;
      *)
        warn "unknown sandbox provider; background jobs stay off" ;;
    esac
  fi

  # --- Direct apps -------------------------------------------------------
  if has_app "gmail" || has_app "calendar" || has_app "drive" || has_app "docs"; then
    say ""
    say "Google direct apps need an OAuth client you own. Recipe (from the repo):"
    say "  1. Create or select a project: https://console.cloud.google.com/apis/credentials"
    say "  2. Enable the Gmail API and Google Calendar API under APIs and Services, then Library."
    say "  3. For Drive, also enable the Google Drive API and Google Picker API."
    say "  4. On the OAuth consent screen choose External; add your account as a test user; publish to Production so refresh tokens do not expire in 7 days."
    say "  5. Create credentials, OAuth client ID, Web application."
    say "  6. Authorized redirect URI: the exact value printed after deploy."
    say "  7. Authorized JavaScript origin: the deployed origin (for the Drive picker)."
    open_url "https://console.cloud.google.com/apis/credentials"
    ask_secret GOOGLE_CLIENT_ID "Google OAuth client ID"
    ask_secret GOOGLE_CLIENT_SECRET "Google OAuth client secret"
    if has_app "drive" || has_app "docs"; then
      say "  Drive and Docs also use the Google Picker key and app id."
      open_url "https://console.cloud.google.com/apis/credentials"
      ask_secret GOOGLE_PICKER_KEY "Google Picker API key"
      ask_secret GOOGLE_PICKER_APP_ID "Google Picker app id"
    fi
  fi
  if has_app "github"; then
    say ""
    say "GitHub can connect with a fine-grained personal access token from the"
    say "/apps walk after deploy, so no OAuth client is required. If you want an"
    say "OAuth app instead, create it and use the redirect URI printed after deploy."
    open_url "https://github.com/settings/developers"
  fi
  if has_app "linkedin"; then
    say ""
    say "LinkedIn needs an OAuth app (tokens last about 60 days)."
    open_url "https://www.linkedin.com/developers/apps"
    ask_secret LINKEDIN_CLIENT_ID "LinkedIn client ID"
    ask_secret LINKEDIN_CLIENT_SECRET "LinkedIn client secret"
  fi

  # --- Chat channels -----------------------------------------------------
  if has_channel "telegram"; then
    say ""
    say "Telegram: create a bot with BotFather and copy the token."
    open_url "https://t.me/BotFather"
    ask_secret TELEGRAM_TOKEN "Telegram bot token"
  fi
  if has_channel "discord"; then
    say ""
    say "Discord: create an application and bot, enable the Message Content intent, copy the token."
    open_url "https://discord.com/developers/applications"
    ask_secret DISCORD_TOKEN "Discord bot token"
  fi

  # --- UptimeRobot -------------------------------------------------------
  if [ "$UPTIME_MODE" = "api" ]; then
    say ""
    say "UptimeRobot API key: dashboard, Integrations and API, API, Create API key."
    open_url "https://dashboard.uptimerobot.com/integrations"
    ask_secret UPTIME_API_KEY "UptimeRobot main API key"
  fi

  # --- Generated secrets -------------------------------------------------
  say ""
  say "AETHER_TOKEN is a shared secret: anyone holding it can read and change"
  say "this instance. Use a long random value and never put it in a URL."
  say "Generating the encryption material."
  if [ -z "$AETHER_ENCRYPTION_KEY" ]; then
    AETHER_ENCRYPTION_KEY=$(gen_key_b64)
    AETHER_ENCRYPTION_KEY_GENERATED=1
    save_secret AETHER_ENCRYPTION_KEY "$AETHER_ENCRYPTION_KEY"
  fi
  if [ -z "$AETHER_TOKEN" ]; then
    AETHER_TOKEN=$(gen_token)
    save_secret AETHER_TOKEN "$AETHER_TOKEN"
  fi
  if [ -z "$TOKEN_ENC_KEY" ]; then
    TOKEN_ENC_KEY=$(gen_key_b64)
    save_secret TOKEN_ENC_KEY "$TOKEN_ENC_KEY"
  fi
  if [ -z "$TOKEN_ENC_KEY_ID" ]; then
    TOKEN_ENC_KEY_ID=$(date +%Y-%m)
    save_secret TOKEN_ENC_KEY_ID "$TOKEN_ENC_KEY_ID"
  fi
  ok "AETHER_ENCRYPTION_KEY: $(mask "$AETHER_ENCRYPTION_KEY")"
  ok "AETHER_TOKEN: $(mask "$AETHER_TOKEN")"
  [ -n "$TOKEN_ENC_KEY" ] && ok "AETHER_TOKEN_ENCRYPTION_KEY: $(mask "$TOKEN_ENC_KEY")"
}

has_app() {
  case ",$(printf '%s' "$APPS" | tr -d ' ')," in *",$1,"*) return 0 ;; *) return 1 ;; esac
}

has_channel() {
  case ",$(printf '%s' "$CHANNELS" | tr -d ' ')," in *",$1,"*) return 0 ;; *) return 1 ;; esac
}

build_config() {
  local embed="gemini-embedding-001" img="$IMAGE_PROVIDER" wb="$WORKBENCH_PROVIDER"
  [ -n "$img" ] || img="none"
  [ -n "$wb" ] || wb="none"
  local tg="false" dc="false"
  has_channel telegram && tg="true"
  has_channel discord && dc="true"
  CONFIG_YAML=$(cat <<YAML
# Generated by scripts/setup.sh. No secrets here; secrets live in Render.
llm:
  provider: $LLM_PROVIDER
  model: $LLM_MODEL
  embedding_model: $embed
messaging:
  telegram:
    enabled: $tg
  discord:
    enabled: $dc
media:
  image_provider: $img
workbench:
  provider: $wb
providers:
  enabled: true
YAML
)
  CONFIG_B64=$(printf '%s' "$CONFIG_YAML" | b64_nowrap)
}

# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

BUILD_COMMAND='if [ -n "$AETHER_CONFIG_B64" ]; then printf %s "$AETHER_CONFIG_B64" | base64 -d > config.yaml; fi; cd webapp && npm ci && npm run build && cd .. && pip install .'
START_COMMAND='aether'
HEALTH_PATH='/healthz'
PYTHON_VERSION_PIN='3.12.11'
NODE_VERSION_PIN='22.12.0'
DISK_MOUNT='/opt/render/project/src/aether-workspace'

ENV_JSON=""
ENV_KEYS=()
ENV_VALS=()

add_env() {
  local k="$1" v="$2"
  [ -n "$v" ] || return 0
  local e
  e="{\"key\":\"$(json_escape "$k")\",\"value\":\"$(json_escape "$v")\"}"
  if [ -n "$ENV_JSON" ]; then ENV_JSON="$ENV_JSON,$e"; else ENV_JSON="$e"; fi
  ENV_KEYS+=("$k")
  ENV_VALS+=("$v")
}

build_env_vars() {
  ENV_JSON=""
  ENV_KEYS=()
  ENV_VALS=()
  add_env DATABASE_URL "$DATABASE_URL"
  add_env AETHER_ENCRYPTION_KEY "$AETHER_ENCRYPTION_KEY"
  add_env AETHER_TOKEN "$AETHER_TOKEN"
  add_env AETHER_CONFIG_B64 "$CONFIG_B64"
  add_env PYTHON_VERSION "$PYTHON_VERSION_PIN"
  add_env NODE_VERSION "$NODE_VERSION_PIN"
  [ -n "$SERVICE_URL" ] && add_env AETHER_PUBLIC_URL "$SERVICE_URL"
  add_env AETHER_TOKEN_ENCRYPTION_KEY "$TOKEN_ENC_KEY"
  add_env AETHER_TOKEN_ENCRYPTION_KEY_ID "$TOKEN_ENC_KEY_ID"
  add_env ANTHROPIC_API_KEY "$ANTHROPIC_KEY"
  add_env OPENAI_API_KEY "$OPENAI_KEY"
  add_env GEMINI_API_KEY "$GEMINI_KEY"
  add_env NVIDIA_API_KEY "$NVIDIA_KEY"
  add_env TAVILY_API_KEY "$TAVILY_KEY"
  add_env GROQ_API_KEY "$GROQ_KEY"
  case "$WORKBENCH_PROVIDER" in
    modal) add_env MODAL_TOKEN_ID "$MODAL_TOKEN_ID"; add_env MODAL_TOKEN_SECRET "$MODAL_TOKEN_SECRET" ;;
    vercel) add_env VERCEL_TOKEN "$VERCEL_TOKEN" ;;
  esac
  add_env TELEGRAM_BOT_TOKEN "$TELEGRAM_TOKEN"
  add_env DISCORD_BOT_TOKEN "$DISCORD_TOKEN"
  add_env GOOGLE_CLIENT_ID "$GOOGLE_CLIENT_ID"
  add_env GOOGLE_CLIENT_SECRET "$GOOGLE_CLIENT_SECRET"
  add_env GOOGLE_PICKER_API_KEY "$GOOGLE_PICKER_KEY"
  add_env GOOGLE_PICKER_APP_ID "$GOOGLE_PICKER_APP_ID"
  add_env GITHUB_CLIENT_ID "$GITHUB_CLIENT_ID"
  add_env GITHUB_CLIENT_SECRET "$GITHUB_CLIENT_SECRET"
  add_env LINKEDIN_CLIENT_ID "$LINKEDIN_CLIENT_ID"
  add_env LINKEDIN_CLIENT_SECRET "$LINKEDIN_CLIENT_SECRET"
}

render_api() {
  local method="$1" path="$2" data="${3:-}"
  if [ "$DRY_RUN" -eq 1 ]; then
    say "  [dry-run] Render API $method $path"
    return 0
  fi
  if [ -n "$data" ]; then
    curl -sS --connect-timeout 10 --max-time 60 --retry 2 --retry-delay 2 \
      -X "$method" "$RENDER_API$path" \
      -H "Authorization: Bearer $RENDER_API_KEY" \
      -H "Accept: application/json" \
      -H "Content-Type: application/json" \
      --data-binary "$data"
  else
    curl -sS --connect-timeout 10 --max-time 60 --retry 2 --retry-delay 2 \
      -X "$method" "$RENDER_API$path" \
      -H "Authorization: Bearer $RENDER_API_KEY" \
      -H "Accept: application/json"
  fi
}

find_render_service() {
  # Print the service id for SERVICE_NAME, or nothing.
  local body
  body=$(render_api GET "/services?name=$SERVICE_NAME&limit=20")
  printf '%s' "$body" | tr '}' '\n' | grep -E "\"name\"[[:space:]]*:[[:space:]]*\"$SERVICE_NAME\"" | head -n 1 \
    | json_id srv
}

needs_public_url() {
  # AETHER_PUBLIC_URL matters for OAuth callbacks and the GitHub Actions queue.
  [ -n "$APPS" ] && return 0
  [ "$WORKBENCH_PROVIDER" = "github_actions" ] && return 0
  return 1
}

render_service_url() {
  local body
  body=$(render_api GET "/services/$SERVICE_ID")
  json_field "url" "$body"
}

create_render_service() {
  local owner body
  if [ "$DRY_RUN" -eq 1 ]; then
    say "  [dry-run] would look up the workspace id via GET /owners"
    owner="tea-dryrun"
  else
    body=$(render_api GET "/owners?limit=1")
    owner=$(json_id tea "$body")
  fi
  [ -n "$owner" ] || die "could not read your Render workspace id; check the API key"
  service_state="workspace $owner"

  [ -n "$SERVICE_URL" ] || SERVICE_URL="https://$SERVICE_NAME.onrender.com"
  build_env_vars
  local details
  details="{\"runtime\":\"python\",\"plan\":\"$PLAN\",\"region\":\"$REGION\",\"healthCheckPath\":\"$HEALTH_PATH\",\"envSpecificDetails\":{\"buildCommand\":\"$(json_escape "$BUILD_COMMAND")\",\"startCommand\":\"$(json_escape "$START_COMMAND")\"}"
  if [ "$PLAN" != "free" ]; then
    details="$details,\"disk\":{\"name\":\"workspace\",\"mountPath\":\"$DISK_MOUNT\",\"sizeGB\":1}"
  fi
  details="$details}"
  local payload
  payload="{\"type\":\"web_service\",\"name\":\"$(json_escape "$SERVICE_NAME")\",\"ownerId\":\"$owner\",\"repo\":\"$(json_escape "$REPO_URL")\",\"autoDeploy\":\"yes\",\"envVars\":[$ENV_JSON],\"serviceDetails\":$details}"

  if [ "$DRY_RUN" -eq 1 ]; then
    say "  [dry-run] would POST /services to create '$SERVICE_NAME' on plan '$PLAN' in $REGION"
    SERVICE_ID="srv-dryrun"
    SERVICE_URL="https://$SERVICE_NAME.onrender.com"
    return 0
  fi
  say "  Creating Render service '$SERVICE_NAME'..."
  body=$(render_api POST "/services" "$payload")
  SERVICE_ID=$(json_id srv "$body")
  if [ -z "$SERVICE_ID" ]; then
    say "  Render said:"
    printf '%s\n' "$body" | head -c 1200
    say ""
    die "service creation failed; see the message above"
  fi
  ok "created service $SERVICE_ID"
  save_state SERVICE_ID "$SERVICE_ID"
  open_url "https://dashboard.render.com/web/$SERVICE_ID"
}

update_render_public_url() {
  [ -n "$SERVICE_URL" ] || return 0
  say "  Updating AETHER_PUBLIC_URL to $SERVICE_URL"
  if [ "$DRY_RUN" -eq 1 ]; then
    say "  [dry-run] would PUT /services/$SERVICE_ID/env-vars/AETHER_PUBLIC_URL"
    return 0
  fi
  render_api PUT "/services/$SERVICE_ID/env-vars/AETHER_PUBLIC_URL" \
    "{\"value\":\"$(json_escape "$SERVICE_URL")\"}" >/dev/null
  say "  Public URL updated."
}

trigger_render_deploy() {
  local body
  if [ "$DRY_RUN" -eq 1 ]; then
    say "  [dry-run] would POST /services/$SERVICE_ID/deploys"
    DEPLOY_ID="dep-dryrun"
    return 0
  fi
  say "  Triggering a deploy..."
  body=$(render_api POST "/services/$SERVICE_ID/deploys" "{\"clearCache\":\"do_not_clear\"}")
  DEPLOY_ID=$(json_id dep "$body")
  if [ -n "$DEPLOY_ID" ]; then say "  Deploy $DEPLOY_ID queued."; fi
  [ -n "$DEPLOY_ID" ] || warn "could not read the deploy id; using the latest deploy instead"
}

poll_render_deploy() {
  if [ "$DRY_RUN" -eq 1 ]; then
    say "  [dry-run] would poll the deploy until live"
    return 0
  fi
  say "  Waiting for the deploy (this is the slow part; first build is a few minutes)."
  local body status tries=0
  while [ "$tries" -lt 120 ]; do
    if [ -n "$DEPLOY_ID" ]; then
      body=$(render_api GET "/services/$SERVICE_ID/deploys/$DEPLOY_ID")
    else
      body=$(render_api GET "/services/$SERVICE_ID/deploys?limit=1")
    fi
    status=$(json_field "status" "$body")
    case "$status" in
      live)
        ok "deploy is live"
        return 0 ;;
      build_failed|update_failed|pre_deploy_failed|canceled|deactivated)
        warn "deploy ended with status: $status"
        show_deploy_logs
        return 1 ;;
      "")
        if [ $((tries % 6)) -eq 0 ]; then printf '  ... waiting for the deploy\n'; fi ;;
      *)
        printf '  ... %s\n' "$status" ;;
    esac
    tries=$((tries + 1))
    sleep 10
  done
  warn "timed out waiting for the deploy; check the Render dashboard"
  return 1
}

show_deploy_logs() {
  [ "$DRY_RUN" -eq 1 ] && return 0
  local body
  body=$(render_api GET "/services/$SERVICE_ID/events?limit=30")
  say "  Recent Render events:"
  printf '%s\n' "$body" | tr ',' '\n' | grep -E '"message"|"type"' | sed 's/^[[:space:]]*//' | head -n 30 || true
  say "  Full build log: https://dashboard.render.com/web/$SERVICE_ID/deploys"
}

# --- Blueprint (no API key) -------------------------------------------

write_blueprint() {
  build_env_vars
  local path="$STATE_DIR/render.yaml"
  local disk="" envlines="" k seen="" keys
  if [ "$PLAN" != "free" ]; then
    disk=$'    disk:\n      name: workspace\n      mountPath: '"$DISK_MOUNT"$'\n      sizeGB: 1\n'
  fi
  keys="DATABASE_URL AETHER_ENCRYPTION_KEY AETHER_TOKEN AETHER_CONFIG_B64"
  case "$LLM_PROVIDER" in
    anthropic) keys="$keys ANTHROPIC_API_KEY" ;;
    openai) keys="$keys OPENAI_API_KEY" ;;
    gemini) keys="$keys GEMINI_API_KEY" ;;
    nvidia) keys="$keys NVIDIA_API_KEY" ;;
  esac
  if has_service "web search"; then keys="$keys TAVILY_API_KEY"; fi
  if has_service "voice-note transcription"; then keys="$keys GROQ_API_KEY"; fi
  keys="$keys GEMINI_API_KEY"
  if has_service "background jobs"; then
    case "$WORKBENCH_PROVIDER" in
      modal) keys="$keys MODAL_TOKEN_ID MODAL_TOKEN_SECRET" ;;
      vercel) keys="$keys VERCEL_TOKEN VERCEL_TEAM_ID VERCEL_PROJECT_ID" ;;
      github_actions) keys="$keys GITHUB_ACTIONS_TOKEN" ;;
    esac
  fi
  keys="$keys AETHER_TOKEN_ENCRYPTION_KEY AETHER_TOKEN_ENCRYPTION_KEY_ID"
  if has_app gmail || has_app calendar || has_app drive || has_app docs; then
    keys="$keys GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET"
    if has_app drive || has_app docs; then keys="$keys GOOGLE_PICKER_API_KEY GOOGLE_PICKER_APP_ID"; fi
  fi
  if has_app github; then keys="$keys GITHUB_CLIENT_ID GITHUB_CLIENT_SECRET"; fi
  if has_app linkedin; then keys="$keys LINKEDIN_CLIENT_ID LINKEDIN_CLIENT_SECRET"; fi
  if has_channel telegram; then keys="$keys TELEGRAM_BOT_TOKEN"; fi
  if has_channel discord; then keys="$keys DISCORD_BOT_TOKEN"; fi
  envlines="      - key: PYTHON_VERSION
        value: '$PYTHON_VERSION_PIN'
      - key: NODE_VERSION
        value: '$NODE_VERSION_PIN'
"
  for k in $keys; do
    case " $seen " in *" $k "*) continue ;; esac
    seen="$seen $k"
    envlines="$envlines      - key: $k
        sync: false
"
  done
  {
    printf '%s\n' \
      "# Generated by scripts/setup.sh for Aether. No secrets live here." \
      "# Commit this as render.yaml in your fork, or create a Blueprint from" \
      "# the repo. Values marked sync: false are entered in the Render" \
      "# Dashboard and stored there." \
      "services:" \
      "  - type: web" \
      "    name: $SERVICE_NAME" \
      "    runtime: python" \
      "    plan: $PLAN" \
      "    region: $REGION"
    printf "    buildCommand: '%s'\n" "$BUILD_COMMAND"
    printf "    startCommand: '%s'\n" "$START_COMMAND"
    printf '    healthCheckPath: %s\n' "$HEALTH_PATH"
    [ -n "$disk" ] && printf '%s' "$disk"
    printf '    envVars:\n'
    printf '%s' "$envlines"
    printf '      - key: AETHER_PUBLIC_URL\n        fromService:\n          name: %s\n          type: web\n          envVarKey: RENDER_EXTERNAL_URL\n' "$SERVICE_NAME"
  } > "$path"
  ok "Blueprint written to $path"
  say ""
  if [ "$DEPLOY_SOURCE" = "public" ]; then
    say "Dashboard route (public repo):"
    say "  1. Open https://dashboard.render.com/blueprints and connect the public repo."
    say "  2. Render reads the complete render.yaml already committed at the repo root."
    say "  3. Paste the prompted sync:false values. Core secrets are in $SECRETS_FILE (chmod 600)."
    say "  4. AETHER_CONFIG_B64 is printed below; it is not secret."
  else
    say "Dashboard route (fork):"
    say "  1. Push $path as render.yaml to your fork."
    say "  2. Open https://dashboard.render.com/blueprints and connect the fork."
    say "  3. Render prompts only for the variables this plan uses."
    say "     The generated secrets are in $SECRETS_FILE (chmod 600)."
  fi
  open_url "https://dashboard.render.com/blueprints"
  say ""
  say "  The non-secret AETHER_CONFIG_B64 value to paste is printed now:"
  printf '%s\n' "  AETHER_CONFIG_B64=$CONFIG_B64"
  SERVICE_URL="https://$SERVICE_NAME.onrender.com"
  save_state SERVICE_URL "$SERVICE_URL"
}

deploy_dashboard_route() {
  # When there is no API key, we cannot create or poll the service. Walk through
  # the dashboard, then continue with verification against the predicted URL.
  warn "No Render API key: creating the service in the dashboard."
  write_blueprint
  say ""
  say "After the dashboard deploy finishes, this script will verify the URL:"
  say "  $SERVICE_URL"
}

keep_deployed_encryption_key() {
  # The content key is not rotatable in place: every encrypted row was
  # written under it. A re-run from a machine without the resume file used
  # to generate a new key and PUT it over the deployed one, making all old
  # chat, memory, and trace rows unreadable. Prefer the deployed key unless
  # the operator explicitly asks to rotate it.
  [ "$ROTATE_ENCRYPTION_KEY" -eq 1 ] && return 0
  [ "$DRY_RUN" -eq 1 ] && return 0
  [ -n "$SERVICE_ID" ] || return 0
  [ -n "$RENDER_API_KEY" ] || return 0
  local body existing
  body=$(render_api GET "/services/$SERVICE_ID/env-vars/AETHER_ENCRYPTION_KEY" 2>/dev/null || true)
  existing=$(json_field "value" "$body")
  if [ -z "$existing" ]; then
    if [ "$AETHER_ENCRYPTION_KEY_GENERATED" -eq 1 ]; then
      warn "could not read the deployed encryption key"
      warn "if this service already has data, stop and restore the original key"
    fi
    return 0
  fi
  [ "$existing" = "$AETHER_ENCRYPTION_KEY" ] && return 0
  if [ "$AETHER_ENCRYPTION_KEY_GENERATED" -eq 1 ] || [ -z "$AETHER_ENCRYPTION_KEY" ]; then
    ok "kept the deployed encryption key so existing data stays readable"
  else
    warn "the deployed service already has a different encryption key"
    warn "keeping the deployed one — pass --rotate-encryption-key to replace it"
  fi
  AETHER_ENCRYPTION_KEY="$existing"
  save_secret AETHER_ENCRYPTION_KEY "$AETHER_ENCRYPTION_KEY"
}

update_existing_env() {
  local i
  for i in "${!ENV_KEYS[@]}"; do
    [ "$DRY_RUN" -eq 1 ] && continue
    render_api PUT "/services/$SERVICE_ID/env-vars/${ENV_KEYS[$i]}" \
      "{\"value\":\"$(json_escape "${ENV_VALS[$i]}")\"}" >/dev/null
  done
  ok "updated environment variables on the existing service"
}

# ---------------------------------------------------------------------------
# UptimeRobot
# ---------------------------------------------------------------------------

ur_call() {
  local endpoint="$1"; shift
  curl -sS -X POST "$UPTIMEROBOT_API/$endpoint" \
    -H "cache-control: no-cache" \
    --data-urlencode "api_key=$UPTIME_API_KEY" \
    --data-urlencode "format=json" \
    "$@"
}

ur_find_monitor() {
  local body
  body=$(ur_call getMonitors --data-urlencode "limit=50")
  printf '%s' "$body" | tr '{' '\n' \
    | grep -F "$SERVICE_URL/healthz" \
    | head -n 1 \
    | grep -oE '"id"[[:space:]]*:[[:space:]]*[0-9]+' | head -n 1 | sed 's/.*:[[:space:]]*//'
}

ur_monitor_status() {
  local id="$1" body
  body=$(ur_call getMonitors --data-urlencode "monitors=$id")
  printf '%s' "$body" | tr '{' '\n' | grep -E "\"id\"[[:space:]]*:[[:space:]]*$id([^0-9]|$)" | head -n 1 \
    | grep -oE '"status"[[:space:]]*:[[:space:]]*[0-9]+' | head -n 1 | sed 's/.*:[[:space:]]*//'
}

uptime_ensure_monitor() {
  local target="$SERVICE_URL/healthz" body existing
  if [ "$DRY_RUN" -eq 1 ]; then
    say "  [dry-run] would create or update an UptimeRobot keyword monitor on $target"
    MONITOR_ID="dryrun"
    return 0
  fi
  [ -n "$UPTIME_API_KEY" ] || return 0
  existing=$(ur_find_monitor)
  say "  Alerts go to the email on your UptimeRobot account; make sure it is the address you want."
  if [ -n "$existing" ]; then
    MONITOR_ID="$existing"
    say "  Updating existing monitor $MONITOR_ID"
    body=$(ur_call editMonitor \
      --data-urlencode "id=$MONITOR_ID" \
      --data-urlencode "friendly_name=aether $SERVICE_NAME" \
      --data-urlencode "url=$target" \
      --data-urlencode "interval=300")
  else
    say "  Creating keyword monitor on $target"
    body=$(ur_call newMonitor \
      --data-urlencode "friendly_name=aether $SERVICE_NAME" \
      --data-urlencode "url=$target" \
      --data-urlencode "type=2" \
      --data-urlencode "keyword_type=1" \
      --data-urlencode "keyword_value=ok" \
      --data-urlencode "interval=300")
    MONITOR_ID=$(printf '%s' "$body" | grep -oE '"id"[[:space:]]*:[[:space:]]*[0-9]+' | head -n 1 | sed 's/.*:[[:space:]]*//')
  fi
  if printf '%s' "$body" | grep -q '"stat":"ok"'; then
    ok "monitor ready (id ${MONITOR_ID:-unknown})"
    save_state MONITOR_ID "$MONITOR_ID"
  else
    warn "UptimeRobot said: $(printf '%s' "$body" | head -c 300)"
    warn "Falling back to the manual steps."
    uptime_manual
  fi
}

uptime_email_flow() {
  local target="$SERVICE_URL/healthz"
  if [ "$DRY_RUN" -eq 1 ]; then
    say "  [dry-run] would submit the UptimeRobot no-API-key monitor request"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    say "  Submitting the no-API-key monitor request (proof of work)..."
    if python3 - "$target" "$ALERT_EMAIL" <<'PY'
import hashlib, json, sys, urllib.parse, urllib.request
url, email = sys.argv[1], sys.argv[2]
base = "https://api.uptimerobot.com/agentic/agent-monitor"
q = urllib.parse.urlencode({"email": email, "url": url})
with urllib.request.urlopen(base + "/challenge?" + q, timeout=30) as r:
    ch = json.load(r)
nonce, ts, diff, sig = ch["nonce"], ch["timestamp"], ch["difficulty"], ch["signature"]
counter = 0
while True:
    h = hashlib.sha256(f"{nonce}|{counter}".encode()).digest()
    z = 0
    for b in h:
        if b == 0:
            z += 8
        else:
            z += 8 - b.bit_length()
            break
    if z >= diff:
        break
    counter += 1
body = json.dumps({"email": email, "url": url, "nonce": nonce,
                   "timestamp": ts, "counter": counter, "signature": sig}).encode()
req = urllib.request.Request(base, data=body, headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=30) as r:
    print(r.read().decode())
PY
    then
      ok "request accepted; check $ALERT_EMAIL for the UptimeRobot activation link"
    else
      warn "the request failed; opening the browser flow instead"
      open_url "https://uptimerobot.com/quick-start?url=$(urlencode "$target")&email=$(urlencode "$ALERT_EMAIL")"
    fi
  else
    say "  Opening the UptimeRobot browser flow (no API key needed)."
    open_url "https://uptimerobot.com/quick-start?url=$(urlencode "$target")&email=$(urlencode "$ALERT_EMAIL")"
  fi
  say "  Your inbox: click Activate in the email from UptimeRobot (link expires in 24h)."
}

uptime_manual() {
  say "  Manual monitoring steps:"
  say "    1. Open https://uptimerobot.com and create a free account."
  say "    2. Add a New Monitor, monitor type Keyword."
  say "    3. URL: $SERVICE_URL/healthz"
  say "    4. Keyword type: exists; keyword: ok"
  say "    5. Interval: 5 minutes (free plan minimum)."
  say "    6. Save. The 5-minute check keeps the free instance from spinning down."
  open_url "https://dashboard.uptimerobot.com/integrations"
}

run_uptime() {
  case "$UPTIME_MODE" in
    api) uptime_ensure_monitor ;;
    email) uptime_email_flow ;;
    manual) uptime_manual ;;
    skip) say "  Uptime monitoring skipped at your request." ;;
  esac
}

verify_monitor() {
  [ -n "$UPTIME_API_KEY" ] || return 0
  [ "$UPTIME_MODE" = "api" ] || return 0
  [ "$DRY_RUN" -eq 1 ] && return 0
  [ -n "$MONITOR_ID" ] || return 0
  local status tries=0
  while [ "$tries" -lt 12 ]; do
    status=$(ur_monitor_status "$MONITOR_ID")
    case "$status" in
      2) ok "monitor reports UP"; return 0 ;;
      8|9) warn "monitor reports DOWN; the service may still be starting"; return 0 ;;
      *) : ;;
    esac
    tries=$((tries + 1))
    sleep 10
  done
  say "  Monitor created; its first check (up to 5 minutes) is still pending."
}

# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

verify_health() {
  local body tries=0
  if [ "$DRY_RUN" -eq 1 ]; then say "  [dry-run] would verify $SERVICE_URL/healthz"; return 0; fi
  say "  Checking $SERVICE_URL/healthz ..."
  while [ "$tries" -lt 40 ]; do
    body=$(curl -fsS --max-time 25 "$SERVICE_URL/healthz" 2>/dev/null || true)
    if printf '%s' "$body" | tr -d ' \n\r\t' | grep -q '"ok":true'; then
      ok "healthz returned {\"ok\":true}"; return 0
    fi
    tries=$((tries + 1))
    sleep 10
  done
  warn "healthz did not return ok in time"
  if [ -n "$SERVICE_ID" ] && [ -n "$RENDER_API_KEY" ]; then
    local events
    events=$(render_api GET "/services/$SERVICE_ID/events?limit=40")
    if printf '%s' "$events" | grep -q 'DATABASE_URL is not set'; then
      warn "The app is failing because DATABASE_URL is not set on the Render service."
      say "  Fix: Render Dashboard, the service, Environment: add DATABASE_URL,"
      say "  AETHER_ENCRYPTION_KEY, AETHER_TOKEN, AETHER_CONFIG_B64, PYTHON_VERSION=3.12.11,"
      say "  NODE_VERSION=22.12.0, then Save and deploy. Or re-run this script."
    fi
  fi
  return 1
}

verify_panel() {
  if [ "$DRY_RUN" -eq 1 ]; then say "  [dry-run] would verify the panel at $SERVICE_URL/"; return 0; fi
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 25 "$SERVICE_URL/" || true)
  case "$code" in
    200) ok "panel is served (HTTP 200)" ;;
    503) warn "panel build is missing (HTTP 503); the buildCommand's npm step likely failed" ;;
    *) warn "panel returned HTTP $code" ;;
  esac
}

verify_migrations() {
  [ "$DRY_RUN" -eq 1 ] && return 0
  [ -n "$SERVICE_ID" ] || return 0
  local body
  body=$(render_api GET "/services/$SERVICE_ID/events?limit=40")
  if printf '%s' "$body" | grep -qi 'database ready' || printf '%s' "$body" | grep -qi 'migrations applied'; then
    ok "migrations ran (the boot log shows the database ready line)"
  else
    say "  Migrations run on boot; confirm in the Render logs:"
    say "  https://dashboard.render.com/web/$SERVICE_ID/logs"
  fi
}

verify_token() {
  [ "$DRY_RUN" -eq 1 ] && return 0
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 25 \
    -H "x-aether-token: $AETHER_TOKEN" "$SERVICE_URL/api/config" || true)
  if [ "$code" = "200" ]; then
    ok "the AETHER_TOKEN is accepted by the API"
  else
    warn "could not confirm the token against /api/config (HTTP $code)"
  fi
}

# ---------------------------------------------------------------------------
# Local copy, backup file, summary
# ---------------------------------------------------------------------------

local_copy_steps() {
  [ "$LOCAL_COPY" = "yes" ] || return 0
  say ""
  hr
  say "Local copy for testing"
  hr
  say "  git clone $REPO_URL && cd Aether"
  say "  python -m venv .venv && source .venv/bin/activate"
  say "  pip install -e \".[dev]\""
  say "  cd webapp && npm ci && npm run build && cd .."
  say "  DATABASE_URL=... AETHER_ENCRYPTION_KEY=... AETHER_TOKEN=... aether"
  say "  Same database and encryption key as the deployed instance to see the same data."
}

write_backup() {
  # Offer to save an owner-only backup of the secrets. Losing the encryption
  # key makes stored data unrecoverable, so this matters.
  local path="$HOME/aether-secrets.env"
  if [ "$DRY_RUN" -eq 1 ]; then
    say "  [dry-run] would offer an owner-only backup at $path"
    return 0
  fi
  if [ "$NON_INTERACTIVE" -eq 0 ]; then
    local ans
    printf 'Save an owner-only backup of the secrets to %s? [Y/n]: ' "$path"
    IFS= read -r ans || true
    case "$ans" in [Nn]*) say "  Skipped. Back up the encryption key somewhere safe yourself."; return 0 ;; esac
  else
    return 0
  fi
  umask 077
  {
    printf '# Aether deploy secrets. Keep this private. Losing AETHER_ENCRYPTION_KEY loses the data.\n'
    printf 'AETHER_ENCRYPTION_KEY=%s\n' "$AETHER_ENCRYPTION_KEY"
    printf 'AETHER_TOKEN=%s\n' "$AETHER_TOKEN"
    printf 'DATABASE_URL=%s\n' "$DATABASE_URL"
    [ -n "$TOKEN_ENC_KEY" ] && printf 'AETHER_TOKEN_ENCRYPTION_KEY=%s\n' "$TOKEN_ENC_KEY"
    [ -n "$TOKEN_ENC_KEY_ID" ] && printf 'AETHER_TOKEN_ENCRYPTION_KEY_ID=%s\n' "$TOKEN_ENC_KEY_ID"
    printf 'SERVICE_URL=%s\n' "$SERVICE_URL"
  } > "$path"
  chmod 600 "$path"
  ok "wrote owner-only backup to $path (chmod 600)"
}

summary() {
  say ""
  hr
  say "Done"
  hr
  say "  Live URL:   $SERVICE_URL"
  say "  Panel:      $SERVICE_URL  (paste the token under Settings > Access)"
  say "  Health:     $SERVICE_URL/healthz"
  [ -n "$SERVICE_ID" ] && say "  Render:     https://dashboard.render.com/web/$SERVICE_ID"
  say "  AETHER_TOKEN: $(mask "$AETHER_TOKEN")"
  say "  Back up AETHER_ENCRYPTION_KEY outside Render now (password manager)."
  say "  Losing it makes every stored message, memory, and token unrecoverable."
  if [ "$NON_INTERACTIVE" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
    local reveal
    printf '  Reveal the token now? [y/N]: '
    IFS= read -r reveal || true
    case "$reveal" in [Yy]*) say "  AETHER_TOKEN=$AETHER_TOKEN" ;; esac
  fi
  say ""
  say "Created or updated:"
  say "  - Render web service '$SERVICE_NAME' (plan $PLAN, region $REGION)"
  say "  - config.yaml delivered as AETHER_CONFIG_B64 (no secrets, not in the repo)"
  case "$UPTIME_MODE" in
    api|email) say "  - UptimeRobot monitor on $SERVICE_URL/healthz (5-minute checks)" ;;
    manual) say "  - manual UptimeRobot steps printed above" ;;
    skip) say "  - no uptime monitor" ;;
  esac
  say ""
  say "Skipped:"
  [ -z "$SERVICES" ] && say "  - optional services (none chosen)"
  [ -z "$APPS" ] && say "  - direct apps (none chosen)"
  [ -z "$CHANNELS" ] && say "  - chat channels (none chosen)"
  [ "$UPTIME_MODE" = "skip" ] && say "  - uptime monitoring"
  say ""
  say "Plan limits to remember:"
  if [ "$PLAN" = "free" ]; then
    say "  - Render free: spins down after 15 idle minutes (the monitor prevents"
    say "    that), 750 instance-hours/month, and an ephemeral disk. The workspace"
    say "    files (IDENTITY.md, SOUL.md, MEMORY.md, daily notes) live only on that"
    say "    disk, so they reset to defaults on any restart. Everything in Postgres"
    say "    (chat, memory events, settings, tasks, approvals) survives."
  else
    say "  - Paid plan: no spin-down and a 1 GB persistent disk keeps the workspace."
  fi
  case "$DATABASE_MODE" in
    render) say "  - Render free Postgres expires 30 days after creation; move to Neon before then." ;;
    neon) say "  - Neon free: 0.5 GB storage and 100 compute-hours/month, no expiry." ;;
  esac
  say ""
  say "What monitoring does not do: it prevents idle spin-down and alerts on"
  say "downtime. It does not stop Render from restarting the service and it does"
  say "not preserve files stored only on the instance disk."
  say ""
  say "Next three things to try:"
  say "  1. Open the panel, Settings > Access, paste the token, send /status."
  say "  2. Send /apps and connect one direct app (the walk gives the link and steps)."
  say "  3. Ask a real question, and watch the Audit Log and Decision Traces pages."
  say ""
  say "Secrets for a resume live in $SECRETS_FILE (chmod 600)."
}

print_redirect_uris() {
  # The code's callback route is /api/oauth/<provider>/callback (runtime.py).
  [ -n "$APPS" ] || return 0
  local any=0
  say ""
  hr
  say "OAuth redirect URIs to register"
  hr
  if has_app gmail || has_app calendar || has_app drive || has_app docs; then
    any=1
    say "  Google:   $SERVICE_URL/api/oauth/google/callback"
    say "  Origin:   $SERVICE_URL  (authorized JavaScript origin for the Drive picker)"
    open_url "https://console.cloud.google.com/apis/credentials"
  fi
  if has_app github; then
    any=1
    say "  GitHub:   $SERVICE_URL/api/oauth/github/callback"
    open_url "https://github.com/settings/developers"
  fi
  if has_app linkedin; then
    any=1
    say "  LinkedIn: $SERVICE_URL/api/oauth/linkedin/callback"
    open_url "https://www.linkedin.com/developers/apps"
  fi
  if [ "$any" -eq 1 ] && [ "$NON_INTERACTIVE" -eq 0 ]; then
    say ""
    say "Register these exact values, then return here."
    printf 'Press Enter once the redirect URIs are registered: '
    IFS= read -r _ || true
  fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

on_interrupt() {
  say ""
  warn "Interrupted. Nothing was deleted."
  if [ -s "$STATE_FILE" ]; then
    say "  Resume with: $0 --force"
  else
    say "  Resume with: $0"
  fi
  if [ -n "$SERVICE_ID" ]; then say "  Render service: $SERVICE_ID ($SERVICE_NAME)"; fi
  if [ -n "$SERVICE_URL" ]; then say "  URL: $SERVICE_URL"; fi
  say "  State: $STATE_FILE"
  exit 130
}
trap on_interrupt INT

main() {
  ask_questions
  gather_credentials
  if [ -z "$DATABASE_URL" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
      warn "DATABASE_URL is empty; a real run would stop here."
    else
      die "DATABASE_URL is required. Re-run and paste the Neon, existing, or Render Postgres connection string."
    fi
  fi
  build_config

  say ""
  hr
  say "Deploying to Render"
  hr
  if [ -n "$RENDER_API_KEY" ] && [ "$DEPLOY_SOURCE" = "fork" ]; then
    say "  Using the Render API (own fork detected)."
    # Reuse the id from a previous run when it still exists. Render may append
    # a suffix to the onrender.com subdomain, so name lookup can miss it.
    if [ -n "$SERVICE_ID" ] && [ "$DRY_RUN" -eq 0 ]; then
      if ! render_api GET "/services/$SERVICE_ID" | grep -q '"id"'; then
        SERVICE_ID=""
      fi
    fi
    [ -n "$SERVICE_ID" ] || SERVICE_ID=$(find_render_service)
    if [ -n "$SERVICE_ID" ]; then
      ok "found existing service $SERVICE_ID (updating, not creating)"
      SERVICE_URL=$(render_service_url)
      save_state SERVICE_URL "$SERVICE_URL"
      keep_deployed_encryption_key
      build_env_vars
      update_existing_env
      trigger_render_deploy
    else
      create_render_service
      local real_url
      real_url=$(render_service_url)
      if [ -n "$real_url" ] && [ "$real_url" != "$SERVICE_URL" ]; then
        SERVICE_URL="$real_url"
        update_render_public_url
        if needs_public_url; then
          trigger_render_deploy
        else
          say "  No direct apps or GitHub Actions queue: the URL is stored; no second deploy needed."
        fi
      elif [ -n "$real_url" ]; then
        SERVICE_URL="$real_url"
      fi
      save_state SERVICE_URL "$SERVICE_URL"
    fi
    poll_render_deploy
  else
    if [ -n "$RENDER_API_KEY" ] && [ "$DEPLOY_SOURCE" = "public" ]; then
      warn "The Render API cannot attach an unconnected public repo, so the"
      warn "dashboard route is used. Everything else still works."
    fi
    deploy_dashboard_route
    say ""
    if [ "$NON_INTERACTIVE" -eq 0 ]; then
      say "Once the dashboard deploy is live, press Enter to continue to verification."
      IFS= read -r _ || true
    else
      say "After the dashboard deploy finishes, verification uses: $SERVICE_URL"
    fi
  fi

  print_redirect_uris

  say ""
  hr
  say "Uptime monitoring"
  hr
  run_uptime

  say ""
  hr
  say "Verification"
  hr
  verify_health || true
  verify_panel || true
  verify_migrations || true
  verify_token || true
  verify_monitor || true

  local_copy_steps
  write_backup
  summary
}

main "$@"
