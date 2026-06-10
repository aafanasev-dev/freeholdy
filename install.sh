#!/bin/bash
# =============================================================================
# install.sh
# One-command installer for freeholdy (API only — no web UI, no SFTPGo).
#
# Runs in TWO auto-detected modes:
#
#   FRESH      — docker and/or nginx are NOT installed. The script provisions
#                them (and the other packages), enables and starts the ones it
#                installed. Intended for a dedicated / empty Ubuntu VPS:
#                  curl -fsSL https://raw.githubusercontent.com/aafanasev-dev/freeholdy/main/install.sh | sudo bash
#                (Use the raw.githubusercontent.com URL — the github.com blob
#                 URL is an HTML page, not the script.)
#
#   COEXIST    — docker AND nginx are already present. The script treats them
#                as prerequisites: it never installs, enables, starts, restarts,
#                upgrades, or apt-touches them, so other apps, containers and
#                vhosts on the box are left untouched. Intended for a server
#                already serving other web apps.
#
# Whatever the mode, every change freeholdy makes is additive and surgical:
#   • nginx sites dirs get group-write + the sticky bit (1775), NON-recursive,
#     so other apps' config files keep their owner/permissions and can't be
#     modified or deleted by freeholdy.
#   • if this nginx includes only conf.d (e.g. nginx.org packages), an additive
#     bridge is dropped in conf.d to wire sites-enabled in — nginx.conf is never
#     edited.
#   • nginx is only ever reloaded gracefully (nginx -t → nginx -s reload), the
#     whole config validated first; if our change fails the test it is reverted,
#     so a running nginx is never left broken.
#   • the API listens on 127.0.0.1 only; public traffic always arrives via nginx.
#   • the service user is reused if it already exists (password untouched).
#
# After detecting the mode the script prints the implications and asks for
# confirmation before making any change.
#
# Options:
#   -u USER   service user to run freeholdy as (default: freeholdy / prompt)
#   -y        assume "yes" to all confirmations (for non-interactive runs)
#   -r        wipe install.log and re-run every step from the beginning
#
# Progress is recorded in install.log next to this script — re-running picks up
# from where it left off and skips steps already marked DONE.
# =============================================================================

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
# Fallback clone (only used when the script is NOT run from a checkout). Uses the
# public HTTPS URL so no SSH key or credentials are needed — this is the path the
# piped one-liner takes. FUTURE: replace with a release-tarball download.
REPO_URL="https://github.com/aafanasev-dev/freeholdy.git"
REPO_BRANCH="main"
SERVICE_USER="freeholdy"          # default; override with -u or the prompt
NGINX_GROUP="nginx-managers"
SERVICE_FILE="/etc/systemd/system/freeholdy.service"
SUDOERS_FILE="/etc/sudoers.d/freeholdy"
APP_PORT_PREFERRED="27182"        # freeholdy default API port; bumped if taken
# nginx config target — resolved at runtime by detect_nginx_target().
NGINX_CONF=""; NGINX_LINK=""; NGINX_BRIDGE=""; NGINX_TARGET_MODE=""
# Acceptable CPython minor versions for freeholdy's venv (inclusive range).
# - MIN: what the application code needs (FastAPI/Pydantic features).
# - MAX: newest interpreter for which our deps (notably pydantic-core) ship
#   prebuilt wheels. Source-building on newer Python pulls in Rust + a matching
#   PyO3, which broke installs on 3.14. Bump MAX when upstream catches up.
PYTHON_MIN_MINOR=11
PYTHON_MAX_MINOR=14

# ── Colours / helpers ──────────────────────────────────────────────────────────
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
ok()      { echo -e "  ${GREEN}✓${NC}  $*"; }
warn()    { echo -e "  ${YELLOW}⚠${NC}  $*"; }
info()    { echo -e "  ${CYAN}→${NC}  $*"; }
fail()    { echo -e "  ${RED}✗${NC}  $*" >&2; }
section() { echo -e "\n${BOLD}━━━  $*  ━━━${NC}"; }

# Run a command as the service user, with its HOME set.
as_user() { sudo -u "$SERVICE_USER" -H "$@"; }

# Derive user-dependent paths (re-run after the service user is resolved).
set_paths() {
    USER_HOME="/home/${SERVICE_USER}"
    APP_DIR="${USER_HOME}/freeholdy"
    VENV_DIR="${APP_DIR}/venv"
}

# Y/N confirmation honouring -y. Without a tty and without -y, abort (fail safe).
confirm() {
    [[ "${ASSUME_YES:-0}" -eq 1 ]] && return 0
    if [[ ! -r /dev/tty ]]; then
        fail "Confirmation needed but no terminal is attached. Re-run interactively or pass -y."
        exit 1
    fi
    local ans
    printf "  %b?%b  %s [y/N]: " "$CYAN" "$NC" "$1" > /dev/tty
    read -r ans < /dev/tty
    [[ "$ans" =~ ^[Yy]([Ee][Ss])?$ ]]
}

# Y/N confirmation defaulting to YES. -y, an empty answer, or no tty all mean yes
# (used for optional add-ons whose default is to install).
confirm_yes() {
    [[ "${ASSUME_YES:-0}" -eq 1 ]] && return 0
    [[ -r /dev/tty ]] || return 0
    local ans
    printf "  %b?%b  %s [Y/n]: " "$CYAN" "$NC" "$1" > /dev/tty
    read -r ans < /dev/tty
    [[ ! "$ans" =~ ^[Nn]([Oo])?$ ]]
}

# Wait (politely) for any other apt/dpkg process to release the lock.
wait_for_apt() {
    command -v fuser &>/dev/null || return 0
    local tries=0
    while fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock \
                /var/lib/apt/lists/lock /var/cache/apt/archives/lock &>/dev/null; do
        tries=$((tries + 1))
        if [[ $tries -gt 60 ]]; then
            warn "apt/dpkg still locked after ~5 min — proceeding and hoping for the best."
            return 0
        fi
        info "Waiting for another apt/dpkg process to release the lock… (${tries})"
        sleep 5
    done
}

# apt-get with lock-wait + retry (handles unattended-upgrades / transient net).
apt_retry() {
    local n=0
    wait_for_apt
    until apt-get "$@"; do
        n=$((n + 1))
        [[ $n -ge 5 ]] && return 1
        warn "apt-get $* failed (attempt ${n}/5) — retrying in 10s (lock or network?)…"
        sleep 10
        wait_for_apt
    done
}

# Highest-minor python3.X on PATH within [MIN, MAX]. Echoes the binary or fails.
resolve_python_bin() {
    local minor bin
    for minor in $(seq "$PYTHON_MAX_MINOR" -1 "$PYTHON_MIN_MINOR"); do
        bin="python3.${minor}"
        if command -v "$bin" &>/dev/null; then echo "$bin"; return 0; fi
    done
    return 1
}

# Highest-minor python3.X apt package within range that apt can provide.
pick_python_apt_candidate() {
    local minor pkg
    for minor in $(seq "$PYTHON_MAX_MINOR" -1 "$PYTHON_MIN_MINOR"); do
        pkg="python3.${minor}"
        if apt-cache show "$pkg" &>/dev/null; then echo "$pkg"; return 0; fi
    done
    return 1
}

# True if TCP port $1 already has a listener (works with or without ss).
port_in_use() {
    local p="$1"
    if command -v ss &>/dev/null; then
        ss -ltnH "( sport = :$p )" 2>/dev/null | grep -q ":$p"
    else
        (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null && { exec 3>&- 3<&-; return 0; }
        return 1
    fi
}

# First free TCP port >= $1.
find_free_port() {
    local p="$1"
    while port_in_use "$p"; do p=$((p + 1)); done
    echo "$p"
}

# Decide WHERE to drop our vhost based on how this nginx is actually wired.
# Both modes use sites-available/sites-enabled (matching the app's own project
# provisioning); conf.d-only builds get an additive bridge so they load.
detect_nginx_target() {
    local dump
    dump="$(nginx -T 2>/dev/null || true)"
    [[ -z "$dump" ]] && dump="$(cat /etc/nginx/nginx.conf 2>/dev/null || true)"
    NGINX_CONF="/etc/nginx/sites-available/freeholdy.conf"
    NGINX_LINK="/etc/nginx/sites-enabled/freeholdy.conf"
    NGINX_BRIDGE=""
    if grep -qE '^[[:space:]]*include[[:space:]]+[^;]*sites-enabled' <<<"$dump"; then
        NGINX_TARGET_MODE="sites-enabled already included"
    elif grep -qE '^[[:space:]]*include[[:space:]]+[^;]*conf\.d' <<<"$dump"; then
        NGINX_TARGET_MODE="conf.d only — adding sites-enabled bridge"
        NGINX_BRIDGE="/etc/nginx/conf.d/zzz-freeholdy-sites-enabled.conf"
    else
        NGINX_TARGET_MODE="include path UNCONFIRMED — adding conf.d bridge as best effort"
        NGINX_BRIDGE="/etc/nginx/conf.d/zzz-freeholdy-sites-enabled.conf"
    fi
}

# Create the conf.d → sites-enabled bridge (idempotent; only in bridge modes).
ensure_sites_bridge() {
    [[ -n "$NGINX_BRIDGE" ]] || return 0
    if [[ ! -f "$NGINX_BRIDGE" ]]; then
        {
            echo "# Added by freeholdy install.sh — this nginx only includes conf.d,"
            echo "# so wire in sites-enabled (additively, without editing nginx.conf)."
            echo "include /etc/nginx/sites-enabled/*;"
        } > "$NGINX_BRIDGE"
        info "Wrote sites-enabled bridge: ${NGINX_BRIDGE}"
    fi
}

# ── Argument parsing ────────────────────────────────────────────────────────────
RESET=0; ASSUME_YES=0; U_OPT=""
while getopts ":u:yr" opt; do
    case "$opt" in
        u) SERVICE_USER="$OPTARG"; U_OPT="$OPTARG" ;;
        y) ASSUME_YES=1 ;;
        r) RESET=1 ;;
        *) ;;
    esac
done
shift $((OPTIND - 1))
set_paths

# ── Resumable steps: persistent log + skip-if-done framework ───────────────────
# Resolve the directory this script lives in. When piped (curl | sudo bash, or
# bash <(curl …)) there is no real file on disk: $0 is a /dev/fd or /proc/*/fd
# handle whose "directory" can't hold a log. Detect that and keep the resume log
# in a stable, writable location instead so logging — and resume — still work.
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" 2>/dev/null && pwd || true)"
if [[ -z "$SCRIPT_DIR" || "$SCRIPT_DIR" == /proc/* || "$SCRIPT_DIR" == /dev/fd* || ! -w "$SCRIPT_DIR" || ! -f "${SCRIPT_DIR}/install.sh" ]]; then
    SCRIPT_DIR=""                       # not a real on-disk checkout (script was piped)
    LOG_DIR="/var/lib/freeholdy"
    mkdir -p "$LOG_DIR" 2>/dev/null || LOG_DIR="${TMPDIR:-/tmp}"
    LOG_FILE="${LOG_DIR}/install.log"
else
    LOG_FILE="${SCRIPT_DIR}/install.log"
fi

if [[ "$RESET" -eq 1 && -f "$LOG_FILE" ]]; then
    rm -f "$LOG_FILE"
fi

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"; }

step_is_done() {
    [[ -f "$LOG_FILE" ]] && grep -qE "\] DONE ${1}(\$|[[:space:]])" "$LOG_FILE"
}
step_mark_started() { log "STARTED $1"; }
step_mark_skipped() { log "SKIPPED $1"; }
step_mark_done() { local id="$1"; shift; log "DONE ${id}${*:+ $*}"; }

# Replay KEY=VAL pairs saved on DONE lines so resumes don't re-prompt.
restore_saved_vars() {
    [[ -f "$LOG_FILE" ]] || return 0
    local line rest kv key val
    while IFS= read -r line; do
        [[ "$line" == *"DONE "* ]] || continue
        rest="${line#*DONE }"
        [[ "$rest" == *" "* ]] || continue
        rest="${rest#* }"
        for kv in $rest; do
            [[ "$kv" == *=* ]] || continue
            key="${kv%%=*}"; val="${kv#*=}"
            [[ "$key" =~ ^[A-Z_][A-Z0-9_]*$ ]] || continue
            export "$key=$val"
        done
    done < "$LOG_FILE"
}
restore_saved_vars
# An explicit -u on this invocation always wins over a value restored from the log.
[[ -n "$U_OPT" ]] && SERVICE_USER="$U_OPT"
set_paths

trap 'rc=$?; log "ERROR exit=$rc line=${BASH_LINENO[0]:-?} cmd=${BASH_COMMAND}"' ERR
trap '_rc=$?; if [[ $_rc -ne 0 ]]; then echo -e "\n  ${RED}✗${NC}  install.sh aborted (exit ${_rc}). See ${LOG_FILE:-install.log}; fix the cause and re-run to resume." >&2; fi' EXIT

log "INSTALL_RUN start ($([[ "$RESET" -eq 1 ]] && echo "reset" || echo "resume")) user=${SERVICE_USER}"
if [[ -f "$LOG_FILE" ]] && grep -q "DONE " "$LOG_FILE"; then
    info "Resuming from $LOG_FILE — completed steps will be skipped (pass -r to redo)"
fi

# ── 1. OS + root checks ─────────────────────────────────────────────────────────
section "Checking environment"
if [[ $EUID -ne 0 ]]; then
    fail "Please run as root:  sudo bash install.sh   (or: curl -fsSL <url> | sudo bash)"
    exit 1
fi
ok "Running as root"

if [[ ! -f /etc/os-release ]]; then
    fail "Cannot detect OS (/etc/os-release missing). This script requires Ubuntu."
    exit 1
fi
# shellcheck source=/dev/null
source /etc/os-release
if [[ "${ID:-}" != "ubuntu" ]]; then
    fail "This script only supports Ubuntu. Detected: ${PRETTY_NAME:-unknown}"
    exit 1
fi
ok "Ubuntu ${VERSION_ID:-?} detected"

# ── Mode detection (FRESH vs COEXIST) ───────────────────────────────────────────
section "Detecting installation mode"

HAVE_DOCKER=0; HAVE_NGINX=0
command -v docker &>/dev/null && HAVE_DOCKER=1
command -v nginx  &>/dev/null && HAVE_NGINX=1

if [[ "$HAVE_DOCKER" -eq 1 && "$HAVE_NGINX" -eq 1 ]]; then
    MODE="coexist"
else
    MODE="fresh"
fi

# In COEXIST mode the running nginx must already be valid BEFORE we add anything,
# so that a later 'nginx -t' failure can only be our doing. Verbose-fail & stop.
if [[ "$MODE" == "coexist" ]]; then
    if nginx -t &>/dev/null; then
        ok "existing nginx config is valid (baseline established)"
    else
        fail "nginx -t fails on the CURRENT config — refusing to touch a broken nginx."
        fail "Fix the existing config first, then re-run. Full diagnostics:"
        echo "────────────────────────────────────────────────────────────" >&2
        nginx -t 2>&1 | sed 's/^/    /' >&2 || true
        echo "────────────────────────────────────────────────────────────" >&2
        exit 1
    fi
    if docker info &>/dev/null; then
        ok "docker daemon is responding"
    else
        warn "docker is installed but 'docker info' failed — is the daemon running?"
    fi
fi

# Show the mode and its implications, then ask to proceed.
echo ""
if [[ "$MODE" == "fresh" ]]; then
    MISSING=()
    [[ "$HAVE_DOCKER" -eq 0 ]] && MISSING+=("docker")
    [[ "$HAVE_NGINX"  -eq 0 ]] && MISSING+=("nginx")
    echo -e "  ${BOLD}Mode: FRESH provisioning${NC}"
    echo -e "  Missing system service(s): ${YELLOW}${MISSING[*]}${NC}"
    echo -e "  This run ${BOLD}WILL${NC}:"
    echo -e "    • apt-install missing packages, including ${YELLOW}${MISSING[*]}${NC}"
    echo -e "    • enable + start ${BOLD}only the service(s) it installs${NC}"
    echo -e "    • create the service user, an nginx vhost for api.<domain>,"
    echo -e "      a systemd unit, and a nightly cert-renewal cron line"
    echo -e "  Best for a ${BOLD}dedicated / empty${NC} Ubuntu VPS."
else
    echo -e "  ${BOLD}Mode: COEXIST (side-by-side)${NC}"
    echo -e "  docker + nginx are ${GREEN}already present${NC}."
    echo -e "  This run ${BOLD}WILL NOT${NC} install, enable, start, restart, upgrade or"
    echo -e "  apt-touch docker or nginx — existing apps/containers/vhosts are untouched."
    echo -e "  This run ${BOLD}WILL${NC} (additively):"
    echo -e "    • install only supporting packages (git, certbot, python, …)"
    echo -e "    • reuse or create the service user '${SERVICE_USER}'"
    echo -e "    • add an nginx vhost for api.<domain> (reverting on any test failure)"
    echo -e "    • set group-write + sticky bit on nginx sites dirs (non-recursive)"
    echo -e "    • add a systemd unit + a nightly cert-renewal cron line"
fi
echo ""
if ! confirm "Proceed in ${MODE^^} mode?"; then
    fail "Aborted by user before any change was made."
    exit 1
fi
log "MODE=${MODE} confirmed (docker=${HAVE_DOCKER} nginx=${HAVE_NGINX})"

# ── 2. Prompt for service user, domain + email ──────────────────────────────────
section "Configuration"
if step_is_done config; then
    ok "config already complete — USER=${SERVICE_USER}, DOMAIN=${DOMAIN:-?}, EMAIL=${EMAIL:-?}"
    step_mark_skipped config
else
step_mark_started config

if [[ ! -r /dev/tty ]]; then
    fail "No terminal available to prompt. Re-run interactively, e.g.: bash <(curl -fsSL <url>)"
    exit 1
fi

# Service user: respect an explicit -u, otherwise prompt (default 'freeholdy'),
# so an existing dedicated user is reused rather than a duplicate created.
if [[ -z "$U_OPT" ]]; then
    printf "  %b?%b  Service user to run freeholdy as [%s]: " "$CYAN" "$NC" "$SERVICE_USER" > /dev/tty
    read -r INPUT_USER < /dev/tty
    INPUT_USER="${INPUT_USER// /}"
    [[ -n "$INPUT_USER" ]] && SERVICE_USER="$INPUT_USER"
    set_paths
fi
if id "$SERVICE_USER" &>/dev/null; then
    ok "Service user : ${SERVICE_USER} (exists — will be reused)"
else
    ok "Service user : ${SERVICE_USER} (will be created)"
fi

DOMAIN=""
while [[ -z "$DOMAIN" ]]; do
    printf "  %b?%b  Base domain (e.g. example.com): " "$CYAN" "$NC" > /dev/tty
    read -r DOMAIN < /dev/tty
    DOMAIN="${DOMAIN// /}"
    if [[ ! "$DOMAIN" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]]; then
        warn "That doesn't look like a domain (e.g. example.com). Try again."
        DOMAIN=""
    fi
done

DEFAULT_EMAIL="admin@${DOMAIN}"
printf "  %b?%b  Let's Encrypt email [%s]: " "$CYAN" "$NC" "$DEFAULT_EMAIL" > /dev/tty
read -r EMAIL < /dev/tty
EMAIL="${EMAIL:-$DEFAULT_EMAIL}"

ok "Base domain  : ${DOMAIN}"
ok "Certbot email: ${EMAIL}"

step_mark_done config "SERVICE_USER=${SERVICE_USER}" "DOMAIN=${DOMAIN}" "EMAIL=${EMAIL}"
fi

set_paths
API_DOMAIN="api.${DOMAIN}"
ok "API endpoint: ${API_DOMAIN}"

# ── 3. apt packages ─────────────────────────────────────────────────────────────
section "Installing system packages"
if step_is_done system_packages; then
    ok "system_packages already complete — skipping"
    step_mark_skipped system_packages
else
step_mark_started system_packages

export DEBIAN_FRONTEND=noninteractive
# Supporting packages installed in BOTH modes.
APT_PACKAGES=( git certbot python3-certbot-nginx python3 python3-venv python3-pip curl )
# Only a FRESH box provisions the system services themselves.
if [[ "$MODE" == "fresh" ]]; then
    APT_PACKAGES+=( nginx docker.io )
fi

info "Running apt-get update…"
apt_retry update -qq || { fail "apt-get update failed repeatedly (lock or network)."; exit 1; }

# If no supported python is on PATH, queue the best apt candidate (+ -venv).
if ! resolve_python_bin &>/dev/null; then
    PY_PKG="$(pick_python_apt_candidate || true)"
    if [[ -z "$PY_PKG" ]]; then
        fail "No python3.${PYTHON_MIN_MINOR}–python3.${PYTHON_MAX_MINOR} package available in apt."
        fail "Enable a backport repo (e.g. deadsnakes PPA) and re-run."
        exit 1
    fi
    info "No Python 3.${PYTHON_MIN_MINOR}–3.${PYTHON_MAX_MINOR} present — will install $PY_PKG (+ -venv)"
    APT_PACKAGES+=("$PY_PKG" "${PY_PKG}-venv")
fi

# Skip anything already installed (by dpkg, or by an equivalent binary on PATH —
# e.g. docker provided by docker-ce, not docker.io; installing docker.io on top
# would break apt with a pkgProblemResolver conflict).
declare -A PKG_BIN=( [git]=git [nginx]=nginx [certbot]=certbot [python3]=python3 [curl]=curl [docker.io]=docker )
TO_INSTALL=()
for pkg in "${APT_PACKAGES[@]}"; do
    if dpkg -s "$pkg" &>/dev/null; then
        ok "$pkg already installed"; continue
    fi
    bin="${PKG_BIN[$pkg]:-}"
    if [[ -n "$bin" ]] && command -v "$bin" &>/dev/null; then
        ok "$pkg satisfied by existing '$bin' ($(command -v "$bin")) — skipping"; continue
    fi
    TO_INSTALL+=("$pkg")
done

if [[ ${#TO_INSTALL[@]} -eq 0 ]]; then
    ok "All required packages already present"
else
    info "Installing: ${TO_INSTALL[*]}"
    if ! apt_retry install -y "${TO_INSTALL[@]}"; then
        fail "apt-get install failed — re-running once with diagnostics:"
        echo "────────────────────────────────────────────────────────────"
        apt-get install -y -o Debug::pkgProblemResolver=true "${TO_INSTALL[@]}" || true
        echo "────────────────────────────────────────────────────────────"
        fail "Package installation failed. The apt output above names the conflict."
        exit 1
    fi
    ok "Packages installed"
fi

# Enable + start ONLY the system services this run actually installed. Never
# touch docker/nginx that were already on the box (COEXIST), and never start a
# second nginx over one already running outside systemd.
if [[ "$MODE" == "fresh" ]]; then
    for svc in docker nginx; do
        was_present_var="HAVE_${svc^^}"
        [[ "${!was_present_var}" -eq 1 ]] && continue   # it pre-existed — leave it
        systemctl enable "$svc" --quiet
        if systemctl is-active --quiet "$svc"; then
            ok "$svc enabled and running"; continue
        fi
        if [[ "$svc" == "nginx" ]] && pgrep -x nginx &>/dev/null; then
            warn "nginx already running but not systemd-tracked — leaving it in place"
            continue
        fi
        systemctl start "$svc"
        ok "$svc enabled and running"
    done
fi

# Verify the key binaries are on PATH now.
for bin in git nginx certbot docker; do
    if command -v "$bin" &>/dev/null; then
        ok "$bin  ($(command -v "$bin"))"
    else
        fail "$bin not found in PATH after installation — check the apt package"
        exit 1
    fi
done

# Compose v2 plugin. compose-mode projects shell out to `docker compose` (a CLI
# plugin, not a standalone binary), so we gate on the subcommand working rather
# than on a dpkg/PATH check. On Ubuntu 26.04 `docker.io` no longer pulls in the
# plugin, so freshly-installed boxes — and pre-existing COEXIST ones — can be
# missing it; install docker-compose-v2 only when the subcommand is absent so we
# never clobber a docker-ce box that already ships its own bundled plugin.
if docker compose version &>/dev/null; then
    ok "docker compose  ($(docker compose version 2>/dev/null | head -n1))"
else
    info "docker compose plugin missing — installing docker-compose-v2"
    if apt_retry install -y docker-compose-v2 && docker compose version &>/dev/null; then
        ok "docker compose  ($(docker compose version 2>/dev/null | head -n1))"
    else
        fail "Could not provide 'docker compose' — compose-mode projects (e.g. ws-chat) will fail."
        fail "Install the Compose v2 plugin manually, then re-run."
        exit 1
    fi
fi

if ! PYTHON_BIN="$(resolve_python_bin)"; then
    fail "No Python in 3.${PYTHON_MIN_MINOR}–3.${PYTHON_MAX_MINOR} on PATH after install"
    exit 1
fi
ok "venv interpreter: $PYTHON_BIN  ($(command -v "$PYTHON_BIN"))"

step_mark_done system_packages
fi

# On resume the step above is skipped — resolve PYTHON_BIN again.
if [[ -z "${PYTHON_BIN:-}" ]]; then
    if ! PYTHON_BIN="$(resolve_python_bin)"; then
        fail "No Python in 3.${PYTHON_MIN_MINOR}–3.${PYTHON_MAX_MINOR} on PATH. Re-run with -r."
        exit 1
    fi
    ok "venv interpreter: $PYTHON_BIN  ($(command -v "$PYTHON_BIN"))"
fi

# Resolve where our vhost should live (derived; recomputed every run).
detect_nginx_target
ok "nginx vhost target: ${NGINX_CONF}"
info "nginx wiring: ${NGINX_TARGET_MODE}"

# ── 4. Service user (reuse if present, create only if absent) ───────────────────
section "Service user '${SERVICE_USER}'"
if step_is_done service_user; then
    ok "service_user already complete — skipping"
    step_mark_skipped service_user
else
step_mark_started service_user

if id "$SERVICE_USER" &>/dev/null; then
    ok "User '$SERVICE_USER' already exists — reusing it, password left unchanged"
else
    info "User '$SERVICE_USER' does not exist — creating it"
    if [[ ! -r /dev/tty ]]; then
        fail "No terminal available to prompt for the '$SERVICE_USER' password."
        exit 1
    fi
    SVC_PASSWORD=""
    while [[ -z "$SVC_PASSWORD" ]]; do
        printf "  %b?%b  Password for new user '%s': " "$CYAN" "$NC" "$SERVICE_USER" > /dev/tty
        IFS= read -rs SVC_PASSWORD < /dev/tty; echo > /dev/tty
        [[ -z "$SVC_PASSWORD" ]] && { warn "Password cannot be empty."; continue; }
        printf "  %b?%b  Repeat password: " "$CYAN" "$NC" > /dev/tty
        IFS= read -rs SVC_PASSWORD_CONFIRM < /dev/tty; echo > /dev/tty
        if [[ "$SVC_PASSWORD" != "$SVC_PASSWORD_CONFIRM" ]]; then
            warn "Passwords do not match — try again."; SVC_PASSWORD=""
        fi
    done
    useradd --create-home --shell /bin/bash "$SERVICE_USER"
    echo "${SERVICE_USER}:${SVC_PASSWORD}" | chpasswd
    unset SVC_PASSWORD SVC_PASSWORD_CONFIRM
    ok "User '$SERVICE_USER' created with home ${USER_HOME}"
fi
mkdir -p "$USER_HOME"
chown "$SERVICE_USER":"$SERVICE_USER" "$USER_HOME"

step_mark_done service_user
fi

# ── 5. Permissions: docker group + surgical nginx perms + sudoers ───────────────
section "Permissions"
if step_is_done permissions; then
    ok "permissions already complete — skipping"
    step_mark_skipped permissions
else
step_mark_started permissions

# docker group membership is additive. Guard against installs with no 'docker'
# group (rootless / some snap setups) so we don't abort the whole run.
if getent group docker &>/dev/null; then
    if id -nG "$SERVICE_USER" | grep -qw docker; then
        ok "'$SERVICE_USER' already in the docker group"
    else
        usermod -aG docker "$SERVICE_USER"
        ok "'$SERVICE_USER' added to the docker group"
    fi
else
    warn "No 'docker' group on this host (rootless/snap docker?) — skipping group add."
    warn "Ensure '${SERVICE_USER}' can reach the docker socket, or freeholdy can't manage containers."
fi

if getent group "$NGINX_GROUP" &>/dev/null; then
    ok "Group '$NGINX_GROUP' already exists"
else
    groupadd "$NGINX_GROUP"; ok "Group '$NGINX_GROUP' created"
fi
if id -nG "$SERVICE_USER" | grep -qw "$NGINX_GROUP"; then
    ok "'$SERVICE_USER' already in '$NGINX_GROUP'"
else
    usermod -aG "$NGINX_GROUP" "$SERVICE_USER"
    ok "'$SERVICE_USER' added to '$NGINX_GROUP'"
fi

# Grant write on the DIRECTORIES ONLY (non-recursive) + sticky bit (1775). This
# lets freeholdy create + manage its OWN config files, while every existing file
# belonging to another app keeps its owner/permissions and cannot be modified or
# deleted by the freeholdy process. (Safe in both modes; on a fresh box the dirs
# are empty anyway.)
for dir in /etc/nginx/sites-available /etc/nginx/sites-enabled; do
    mkdir -p "$dir"
    chgrp "$NGINX_GROUP" "$dir"     # group of the dir only — owner stays root
    chmod 1775 "$dir"
    ok "Group-write + sticky set on $dir  (root:$NGINX_GROUP 1775, non-recursive)"
done

# ACME webroot. freeholdy issues per-subdomain certs with `certbot certonly --webroot`
# (NOT --nginx) so certbot never rewrites the vhosts freeholdy owns. certbot (run as
# root via sudo) writes challenge tokens here; nginx serves them. root-owned, world-readable.
CERTBOT_WEBROOT="/var/www/certbot"
mkdir -p "${CERTBOT_WEBROOT}/.well-known/acme-challenge"
chmod -R 0755 "$CERTBOT_WEBROOT"
ok "ACME webroot ready at ${CERTBOT_WEBROOT} (root-owned, world-readable)"

# Passwordless sudo for the freeholdy service account.
# This grants the account UNRESTRICTED passwordless sudo (equivalent to
# `${SERVICE_USER} ALL=(ALL:ALL) NOPASSWD: ALL`). The account can run any
# command as any user/group without a password — effectively full root.
# This is a deliberate, broad grant; everything freeholdy needs at runtime
# (nginx validate/reload, certbot issuance, systemctl unit control, the
# bundled cert manager) is covered by it.
cat > "$SUDOERS_FILE" <<EOF
# Managed by install.sh — do not edit manually
# Unrestricted passwordless sudo for the freeholdy service account.
${SERVICE_USER} ALL=(ALL:ALL) NOPASSWD: ALL
EOF
chmod 0440 "$SUDOERS_FILE"
if visudo -c -f "$SUDOERS_FILE" &>/dev/null; then
    ok "Sudoers file written and validated: $SUDOERS_FILE"
else
    fail "Sudoers syntax check failed — removing invalid file"
    rm -f "$SUDOERS_FILE"; exit 1
fi

step_mark_done permissions
fi

# ── 6. Source: use the checkout in place, else copy it, else clone ──────────────
section "freeholdy source"
if step_is_done fetch_source; then
    ok "fetch_source already complete — APP_DIR=${APP_DIR}"
    step_mark_skipped fetch_source
else
step_mark_started fetch_source

if [[ -f "${SCRIPT_DIR}/app/main.py" ]]; then
    if [[ "$SCRIPT_DIR" == "$APP_DIR" ]]; then
        info "Running from the target location — using checkout in place"
    else
        if [[ "$APP_DIR/" == "$SCRIPT_DIR/"* || "$SCRIPT_DIR/" == "$APP_DIR/"* ]]; then
            fail "Source (${SCRIPT_DIR}) and target (${APP_DIR}) are nested — cannot copy safely."
            fail "Clone the repo somewhere outside ${USER_HOME}, or pass -u so paths don't overlap."
            exit 1
        fi
        info "Running from a checkout at ${SCRIPT_DIR} — copying into ${APP_DIR}"
        mkdir -p "$APP_DIR"
        cp -a "${SCRIPT_DIR}/." "$APP_DIR/"
        rm -rf "${APP_DIR}/venv"          # never carry a venv across paths/users
        rm -f  "${APP_DIR}/install.log"   # the resume log belongs to the source dir
        ok "Source copied to $APP_DIR"
    fi
elif [[ -d "${APP_DIR}/.git" ]]; then
    info "Repo already present at ${APP_DIR} — pulling latest…"
    git -c safe.directory="$APP_DIR" -C "$APP_DIR" pull --ff-only
    ok "Repository updated at $APP_DIR"
else
    info "No local checkout found — cloning ${REPO_URL} (branch ${REPO_BRANCH})…"
    git clone -b "$REPO_BRANCH" "$REPO_URL" "$APP_DIR"
    ok "Repository cloned to $APP_DIR"
fi

chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"
ok "Source owned by '$SERVICE_USER'"

step_mark_done fetch_source
fi

# ── 7. API port selection (default 27182; confirm if bumped) ────────────────────
section "API port selection"
if step_is_done port_select; then
    ok "port_select already complete — APP_PORT=${APP_PORT:-?}"
    step_mark_skipped port_select
else
step_mark_started port_select

command -v ss &>/dev/null || warn "ss (iproute2) not found — port-free detection falls back to a loopback probe."
APP_PORT="$(find_free_port "$APP_PORT_PREFERRED")"
if [[ "$APP_PORT" == "$APP_PORT_PREFERRED" ]]; then
    ok "Default port ${APP_PORT} is free — using it"
else
    warn "Default port ${APP_PORT_PREFERRED} is in use by another service."
    warn "Next free port is ${BOLD}${APP_PORT}${NC}."
    if ! confirm "Use port ${APP_PORT} for the freeholdy API?"; then
        fail "Aborted: no port confirmed. Free port ${APP_PORT_PREFERRED} or re-run to pick again."
        exit 1
    fi
    ok "Using port ${APP_PORT}"
fi

step_mark_done port_select "APP_PORT=${APP_PORT}"
fi
APP_PORT="${APP_PORT:-$(find_free_port "$APP_PORT_PREFERRED")}"

# ── 8. .env configuration + runtime directories ─────────────────────────────────
section ".env configuration"
if step_is_done env_file; then
    ok "env_file already complete — skipping"
    step_mark_skipped env_file
else
step_mark_started env_file

cd "$APP_DIR"
if [[ ! -f .env ]]; then
    cp .env.example .env
    ok ".env created from .env.example"
fi
# Apply domain, email, listen host/port (idempotent). HOST stays loopback so the
# API is only reachable through nginx, never directly on a public interface.
sed -i -E "s|^BASE_DOMAIN=.*|BASE_DOMAIN=${DOMAIN}|"   .env
sed -i -E "s|^CERTBOT_EMAIL=.*|CERTBOT_EMAIL=${EMAIL}|" .env
sed -i -E "s|^PORT=.*|PORT=${APP_PORT}|"                .env
sed -i -E "s|^HOST=.*|HOST=127.0.0.1|"                  .env
grep -q '^BASE_DOMAIN='  .env || echo "BASE_DOMAIN=${DOMAIN}"   >> .env
grep -q '^CERTBOT_EMAIL=' .env || echo "CERTBOT_EMAIL=${EMAIL}" >> .env
grep -q '^PORT='          .env || echo "PORT=${APP_PORT}"       >> .env
grep -q '^HOST='          .env || echo "HOST=127.0.0.1"         >> .env
chown "$SERVICE_USER":"$SERVICE_USER" .env
ok "BASE_DOMAIN=${DOMAIN}, CERTBOT_EMAIL=${EMAIL}, HOST=127.0.0.1, PORT=${APP_PORT}"

# Warn if the project container port range overlaps something already listening.
RANGE_START=$(grep -E '^PORT_RANGE_START=' .env | cut -d= -f2 || echo 8100)
RANGE_END=$(grep -E '^PORT_RANGE_END=' .env | cut -d= -f2 || echo 9000)
if command -v ss &>/dev/null; then
    BUSY_IN_RANGE=$(ss -ltnH 2>/dev/null | awk '{print $4}' | sed -E 's/.*:([0-9]+)$/\1/' \
        | awk -v a="$RANGE_START" -v b="$RANGE_END" '$1>=a && $1<=b' | sort -un | tr '\n' ' ' || true)
    if [[ -n "${BUSY_IN_RANGE// /}" ]]; then
        warn "Ports already in use within the project range ${RANGE_START}-${RANGE_END}: ${BUSY_IN_RANGE}"
        warn "freeholdy auto-skips busy ports, but you may narrow PORT_RANGE_* in .env."
    else
        ok "Project port range ${RANGE_START}-${RANGE_END} has no current listeners"
    fi
else
    warn "ss (iproute2) not available — skipping project port-range overlap check."
fi

for dir in data dockerfiles nginx_configs projects compose; do
    mkdir -p "${APP_DIR}/${dir}"
    chown "$SERVICE_USER":"$SERVICE_USER" "${APP_DIR}/${dir}"
done
ok "Runtime directories ready"

step_mark_done env_file
fi

# ── 9. Python venv + dependencies ───────────────────────────────────────────────
section "Python virtual environment"
if step_is_done python_venv; then
    ok "python_venv already complete — skipping"
    step_mark_skipped python_venv
else
step_mark_started python_venv

# Make sure the chosen interpreter can build a venv. A deadsnakes python3.X
# without its matching -venv package fails with "ensurepip is not available".
if ! "$PYTHON_BIN" -c "import ensurepip" &>/dev/null; then
    info "Installing ${PYTHON_BIN}-venv (ensurepip missing for ${PYTHON_BIN})…"
    apt_retry install -y "${PYTHON_BIN}-venv" \
        || warn "Could not install ${PYTHON_BIN}-venv — venv creation may fail below."
fi

if [[ ! -d "$VENV_DIR" ]]; then
    info "Creating venv with $PYTHON_BIN…"
    as_user "$PYTHON_BIN" -m venv "$VENV_DIR"
    ok "venv created at $VENV_DIR"
else
    VENV_PY_VER=$(as_user "${VENV_DIR}/bin/python" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "?")
    EXPECTED_VER=$("$PYTHON_BIN" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    if [[ "$VENV_PY_VER" != "$EXPECTED_VER" ]]; then
        warn "Existing venv uses Python $VENV_PY_VER, expected $EXPECTED_VER — recreating"
        rm -rf "$VENV_DIR"
        as_user "$PYTHON_BIN" -m venv "$VENV_DIR"
        ok "venv recreated at $VENV_DIR with $PYTHON_BIN"
    else
        ok "venv already exists at $VENV_DIR (Python $VENV_PY_VER)"
    fi
fi
info "Installing Python dependencies…"
as_user "${VENV_DIR}/bin/pip" install --quiet --upgrade pip
as_user "${VENV_DIR}/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"
ok "Dependencies installed"

step_mark_done python_venv
fi

# ── 10. nginx reverse proxy for api.<domain> (graceful, revert-on-failure) ──────
section "nginx reverse proxy for ${API_DOMAIN}"

write_http_conf() {
    cat > "$NGINX_CONF" <<EOF
server {
    listen 80;
    server_name ${API_DOMAIN};

    location / {
        proxy_pass         http://127.0.0.1:${APP_PORT};
        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
    }
}
EOF
}

write_ssl_conf() {
    cat > "$NGINX_CONF" <<EOF
server {
    listen 80;
    server_name ${API_DOMAIN};
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl;
    server_name ${API_DOMAIN};

    ssl_certificate     /etc/letsencrypt/live/${API_DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${API_DOMAIN}/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;

    location / {
        proxy_pass         http://127.0.0.1:${APP_PORT};
        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
    }
}
EOF
}

# Validate the WHOLE nginx config and only reload on success; on failure remove
# OUR vhost so a running nginx is never left broken. The conf.d bridge is kept —
# it only includes our own dir and is benign.
apply_and_reload_or_revert() {
    if nginx -t &>/dev/null; then
        nginx -s reload
        return 0
    fi
    fail "nginx config test failed after adding ${API_DOMAIN} — reverting our vhost"
    nginx -t || true
    rm -f "$NGINX_CONF" "$NGINX_LINK"
    nginx -t &>/dev/null && nginx -s reload || true
    return 1
}

if step_is_done nginx_proxy; then
    ok "nginx_proxy already complete — skipping"
    step_mark_skipped nginx_proxy
else
step_mark_started nginx_proxy

ensure_sites_bridge
write_http_conf
ln -sf "$NGINX_CONF" "$NGINX_LINK"
if apply_and_reload_or_revert; then
    ok "HTTP config for ${API_DOMAIN} active (other vhosts untouched)"
    step_mark_done nginx_proxy
else
    fail "Could not enable ${API_DOMAIN} without breaking nginx — left existing config intact."
    exit 1
fi
fi

# ── 11. systemd service (only our own unit) ─────────────────────────────────────
section "systemd service"
if step_is_done systemd_service; then
    ok "systemd_service already complete — skipping"
    step_mark_skipped systemd_service
else
step_mark_started systemd_service

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=freeholdy API
After=network.target docker.service nginx.service

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${VENV_DIR}/bin/uvicorn app.main:app --host 127.0.0.1 --port ${APP_PORT}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
ok "Wrote $SERVICE_FILE  (127.0.0.1:${APP_PORT})"

systemctl daemon-reload
systemctl enable freeholdy --quiet
systemctl restart freeholdy
sleep 2
if systemctl is-active --quiet freeholdy; then
    ok "freeholdy service is running"
    step_mark_done systemd_service     # only mark done once it actually started
else
    warn "freeholdy service is not active — check: journalctl -u freeholdy -e"
    warn "Leaving this step unmarked so a re-run retries it after you fix the cause."
fi
fi

# ── 12. SSL certificate (best-effort, only for api.<domain>) ────────────────────
section "SSL certificate"

CERT_SCRIPT="${APP_DIR}/scripts/cert-manager.sh"
SSL_OK=0

if step_is_done ssl_cert; then
    ok "ssl_cert already complete — skipping"
    step_mark_skipped ssl_cert
    [[ -f "/etc/letsencrypt/live/${API_DOMAIN}/fullchain.pem" ]] && SSL_OK=1
else
step_mark_started ssl_cert

# Point the cert manager at THIS deployment's domain + email only.
if [[ -f "$CERT_SCRIPT" ]]; then
    sed -i -E "s|^( *)\"api\.[^\"]*\"|\1\"${API_DOMAIN}\"|" "$CERT_SCRIPT"
    sed -i -E "s|^CERTBOT_EMAIL=.*|CERTBOT_EMAIL=\"${EMAIL}\"|" "$CERT_SCRIPT"
    chmod +x "$CERT_SCRIPT"
fi

info "Requesting certificate for ${API_DOMAIN} (requires DNS to point here)…"
if [[ -f "$CERT_SCRIPT" ]] && bash "$CERT_SCRIPT"; then :; fi
if [[ -f "/etc/letsencrypt/live/${API_DOMAIN}/fullchain.pem" ]]; then
    write_ssl_conf
    if apply_and_reload_or_revert; then
        SSL_OK=1
        ok "HTTPS enabled for ${API_DOMAIN}"
    else
        warn "SSL nginx config failed test — falling back to HTTP-only"
        write_http_conf
        ln -sf "$NGINX_CONF" "$NGINX_LINK"
        apply_and_reload_or_revert || true
    fi
else
    warn "No certificate yet for ${API_DOMAIN}."
    warn "Point its DNS A record at this server, then run:  sudo ${CERT_SCRIPT}"
    warn "and re-run this installer (or manually enable the SSL nginx block)."
fi

CRON_LINE="0 3 * * * ${CERT_SCRIPT}"
if [[ -f "$CERT_SCRIPT" ]]; then
    ( crontab -l 2>/dev/null | grep -vF "$CERT_SCRIPT" || true; echo "$CRON_LINE" ) | crontab -
    ok "Nightly cert renewal cron installed (03:00)"
fi

[[ "$SSL_OK" -eq 1 ]] && step_mark_done ssl_cert
fi

# ── 13. First API token ─────────────────────────────────────────────────────────
section "First API token"
if step_is_done api_token; then
    ok "api_token already issued (re-run with -r to reissue)"
    step_mark_skipped api_token
else
step_mark_started api_token

echo ""
as_user "${VENV_DIR}/bin/python" "${APP_DIR}/scripts/generate_token.py" generate --name initial
echo ""

step_mark_done api_token
fi

# ── 14. Web UI control panel (optional plugin, default yes) ─────────────────────
# The webui plugin is a managed project served at ui.<domain>. We mint a dedicated
# token and hand it back as a one-click login link (ui.<domain>/token/<TOKEN>):
# the React app captures the token from that path, stores it, and drops the user
# straight into the dashboard — no copy/paste of the token needed.
section "Web UI control panel"
WEBUI_DOMAIN="ui.${DOMAIN}"
WEBUI_LINK=""
if step_is_done webui_plugin; then
    ok "webui_plugin already complete — skipping"
    step_mark_skipped webui_plugin
elif confirm_yes "Install the freeholdy web UI control panel (served at ${WEBUI_DOMAIN})?"; then
    step_mark_started webui_plugin

    # Dedicated token for the web UI — embedded in the login link below.
    WEBUI_TOKEN="$(as_user "${VENV_DIR}/bin/python" "${APP_DIR}/scripts/generate_token.py" \
        generate --name webui 2>/dev/null | grep -oE '[A-Za-z0-9_-]{40,}' | head -n1 || true)"

    if [[ -z "$WEBUI_TOKEN" ]]; then
        warn "Could not generate a web UI token — skipping the web UI install."
        warn "Install it later from the API: POST /plugins/webui/add"
    elif WEBUI_RESP="$(curl -fsS -X POST "http://127.0.0.1:${APP_PORT}/plugins/webui/add" \
            -H "Authorization: Bearer ${WEBUI_TOKEN}" \
            -H "Content-Type: application/json" \
            -d '{"project_name":"webui"}' 2>/dev/null)"; then
        # nginx/SSL is wired synchronously by the add; the container then builds in
        # the background, so the link goes live once that build finishes.
        if grep -q '"ssl_enabled":true' <<<"$WEBUI_RESP"; then
            WEBUI_SCHEME="https"
        else
            WEBUI_SCHEME="http"
        fi
        WEBUI_LINK="${WEBUI_SCHEME}://${WEBUI_DOMAIN}/token/${WEBUI_TOKEN}"
        ok "Web UI plugin provisioned — container is building in the background"
        info "Open the control panel (auto-logs-in via the embedded token):"
        echo -e "      ${CYAN}${WEBUI_LINK}${NC}"
        warn "Treat this link like a password — it grants full API access."
        step_mark_done webui_plugin
    else
        warn "Web UI install request failed (is the API up at 127.0.0.1:${APP_PORT}?)."
        warn "Install it later from the API: POST /plugins/webui/add"
    fi
else
    info "Skipping the web UI — install it later from the API: POST /plugins/webui/add"
fi

# ── Summary ───────────────────────────────────────────────────────────────────────
SCHEME=$([[ "$SSL_OK" -eq 1 ]] && echo "https" || echo "http")
log "INSTALL_RUN end mode=${MODE} ssl_ok=${SSL_OK} port=${APP_PORT}"
echo -e "${BOLD}${GREEN}━━━  freeholdy installed (${MODE} mode)  ━━━${NC}"
echo ""
echo -e "  API            : ${CYAN}${SCHEME}://${API_DOMAIN}${NC}  (docs at /docs)"
echo -e "  Listen address : ${CYAN}127.0.0.1:${APP_PORT}${NC}  (nginx proxies; not exposed directly)"
echo -e "  Service user   : ${CYAN}${SERVICE_USER}${NC}"
echo -e "  Service        : ${CYAN}systemctl status freeholdy${NC}"
echo -e "  Logs           : ${CYAN}journalctl -u freeholdy -f${NC}"
echo -e "  App directory  : ${CYAN}${APP_DIR}${NC}"
echo -e "  Install log    : ${CYAN}${LOG_FILE}${NC}"
echo ""
if [[ "$MODE" == "coexist" ]]; then
    echo -e "  Other apps on this nginx/docker were left untouched."
fi
echo -e "  Save the API token printed above — it is shown only once."
if [[ -n "${WEBUI_LINK:-}" ]]; then
    echo ""
    echo -e "  Web UI         : ${CYAN}${WEBUI_LINK}${NC}"
    echo -e "  ${YELLOW}One-click login link${NC} (token embedded) — live once the container finishes building."
fi
if [[ "$SSL_OK" -ne 1 ]]; then
    echo ""
    echo -e "  ${YELLOW}SSL not yet active.${NC} Point ${API_DOMAIN}'s DNS at this server, then run:"
    echo -e "      ${CYAN}sudo ${CERT_SCRIPT} && sudo bash ${SCRIPT_DIR:-$APP_DIR}/install.sh${NC}"
fi
echo ""
