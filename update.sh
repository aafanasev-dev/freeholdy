#!/bin/bash
# =============================================================================
# update.sh
# In-place upgrade for an already-installed freeholdy.
#
# install.sh is a one-command bootstrap with no upgrade path: its resume log
# marks fetch_source DONE, so re-running it skips the git pull entirely. This
# script is the supported way to move an installed server to a newer revision.
#
# An upgrade is more than "git pull && systemctl restart" for two reasons:
#
#   1. webui and freeholdy-help build FROM the repo tree. plugins/webui/install.sh
#      copies webui/ out of the checkout into the docker build context at install
#      time, so new code does NOT reach them until the plugin is added again --
#      and POST /plugins/{n}/add is a 409 while the project exists. They must be
#      deleted and re-added. Nothing user-authored lives in either project, and
#      the Let's Encrypt certs stay in /etc/letsencrypt so the re-add reuses them.
#
#   2. There is no migrations framework. migrate_db.sh has to run against the new
#      code's schema expectations while the service is stopped.
#
# The sequence is: pick a revision -> remove the two managed plugins -> stop the
# service -> reset the checkout -> refresh deps -> back up + migrate the DB ->
# start the service -> re-add the plugins that were there and wait for the builds.
#
# Options:
#   -u USER   service user (default: auto-detected from the systemd unit)
#   -v REF    revision to update to: "main" or a tag name (skips the prompt)
#   -y        assume "yes" to all confirmations (implies -v main without -v)
#   -l        list available versions and exit (changes nothing)
#
# Everything the operator owns survives: .env, data/, projects/ and the other
# runtime dirs are gitignored, and the reset never passes `clean -x`.
# =============================================================================

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
SERVICE_NAME="freeholdy"
SERVICE_FILE="/etc/systemd/system/freeholdy.service"
DEFAULT_SERVICE_USER="freeholdy"
DEFAULT_BRANCH="main"

# The projects that must be torn down and rebuilt because they stage their docker
# build context out of this repo (see the header). Order matters: the control
# panel is rebuilt first so it comes back sooner.
MANAGED_PLUGINS=( webui freeholdy-help )

HEALTH_TIMEOUT=90      # seconds to wait for GET /health after starting the service
BUILD_TIMEOUT=1800     # seconds to wait for one plugin build (webui runs npm build)
POLL_INTERVAL=5        # seconds between build-status polls

# ── Colours / helpers ──────────────────────────────────────────────────────────
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
ok()      { echo -e "  ${GREEN}✓${NC}  $*"; }
warn()    { echo -e "  ${YELLOW}⚠${NC}  $*"; }
info()    { echo -e "  ${CYAN}→${NC}  $*"; }
fail()    { echo -e "  ${RED}✗${NC}  $*" >&2; }
section() { echo -e "\n${BOLD}━━━  $*  ━━━${NC}"; }

# Run a command as the service user, with its HOME set.
as_user() { sudo -u "$SERVICE_USER" -H "$@"; }

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

# Read one KEY=VALUE out of a .env-format file. Echoes empty when absent.
env_value() {
    local file="$1" key="$2" raw
    [[ -f "$file" ]] || return 0
    raw="$(grep -E "^${key}=" "$file" | head -1 || true)"
    [[ -n "$raw" ]] || return 0
    raw="${raw#${key}=}"
    raw="${raw//\"/}"
    echo "${raw// /}"
}

# Pull a value out of a JSON document on stdin. Never fails the script: an
# unparseable body just yields the empty string, which callers treat as "unknown".
#   json_field '<expr>'   where <expr> is python operating on the parsed `d`
json_field() {
    python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
try:
    v = $1
except Exception:
    sys.exit(0)
if v is True:  print('true')
elif v is False: print('false')
elif v is not None: print(v)
" 2>/dev/null || true
}

# ── Argument parsing ────────────────────────────────────────────────────────────
ASSUME_YES=0; LIST_ONLY=0; U_OPT=""; V_OPT=""
while getopts ":u:v:yl" opt; do
    case "$opt" in
        u) U_OPT="$OPTARG" ;;
        v) V_OPT="$OPTARG" ;;
        y) ASSUME_YES=1 ;;
        l) LIST_ONLY=1 ;;
        \?) fail "Unknown option: -$OPTARG"; exit 1 ;;
        :)  fail "Option -$OPTARG requires an argument"; exit 1 ;;
    esac
done
# -y with no explicit revision means "the default branch", so an unattended run
# never blocks on the version prompt.
[[ "$ASSUME_YES" -eq 1 && -z "$V_OPT" ]] && V_OPT="$DEFAULT_BRANCH"

# ── Recovery trap ───────────────────────────────────────────────────────────────
# Everything before the service is stopped is harmless to abandon. After that the
# box is mid-upgrade, so a failure has to hand the operator a way back rather than
# a bare non-zero exit.
DANGER_ZONE=0          # set to 1 once the service has been stopped
CURRENT_STEP="startup"
DB_BACKUP=""
PREV_SHA=""
TOKEN_ID=""

cleanup() {
    local rc=$?
    # The temporary token must not outlive the run, success or failure.
    if [[ -n "$TOKEN_ID" ]]; then
        as_user "${VENV_DIR}/bin/python" "${APP_DIR}/scripts/generate_token.py" \
            revoke --id "$TOKEN_ID" >/dev/null 2>&1 \
            && TOKEN_ID="" || warn "Could not revoke the temporary API token (id ${TOKEN_ID}) — revoke it by hand."
    fi
    [[ $rc -eq 0 ]] && return 0
    echo ""
    fail "update.sh failed during: ${CURRENT_STEP} (exit ${rc})"
    if [[ "$DANGER_ZONE" -eq 1 ]]; then
        echo -e "     The ${SERVICE_NAME} service may be stopped and the checkout may be on the new revision."
        [[ -n "$DB_BACKUP" ]] && echo -e "     DB backup   : ${CYAN}${DB_BACKUP}${NC}"
        [[ -n "$PREV_SHA"  ]] && echo -e "     Previous rev: ${CYAN}${PREV_SHA}${NC}"
        echo -e "     To roll back:"
        [[ -n "$PREV_SHA" ]] && \
        echo -e "       ${CYAN}sudo -u ${SERVICE_USER} git -C ${APP_DIR} reset --hard ${PREV_SHA}${NC}"
        echo -e "       ${CYAN}sudo -u ${SERVICE_USER} ${VENV_DIR}/bin/pip install -r ${APP_DIR}/requirements.txt${NC}"
        [[ -n "$DB_BACKUP" ]] && \
        echo -e "       ${CYAN}cp ${DB_BACKUP} ${APP_DIR}/data/freeholdy.db${NC}"
        echo -e "       ${CYAN}sudo systemctl start ${SERVICE_NAME}${NC}"
        echo -e "     Then re-add the control panel:  ${CYAN}POST /plugins/webui/add${NC}"
        echo -e "     Service log : ${CYAN}journalctl -u ${SERVICE_NAME} -e${NC}"
    else
        echo -e "     Nothing was changed — the service and its projects are untouched."
    fi
    echo ""
}
trap cleanup EXIT

# ── 0. Root check ───────────────────────────────────────────────────────────────
section "Checking environment"
if [[ $EUID -ne 0 ]]; then
    fail "Please run as root:  sudo bash update.sh"
    exit 1
fi
ok "Running as root"

for bin in git curl python3 systemctl; do
    command -v "$bin" &>/dev/null || { fail "Required command not found: ${bin}"; exit 1; }
done

# ── 1. Discover the existing install ────────────────────────────────────────────
# The systemd unit install.sh wrote is the authoritative record of how this box was
# set up — it carries the user, the app dir and the API port in one place.
CURRENT_STEP="discovering the install"
section "Existing installation"

SERVICE_USER=""; APP_DIR=""; APP_PORT=""
if [[ -f "$SERVICE_FILE" ]]; then
    SERVICE_USER="$(grep -E '^User=' "$SERVICE_FILE" | head -1 | cut -d= -f2- || true)"
    APP_DIR="$(grep -E '^WorkingDirectory=' "$SERVICE_FILE" | head -1 | cut -d= -f2- || true)"
    APP_PORT="$(grep -E '^ExecStart=' "$SERVICE_FILE" | head -1 | grep -oE -- '--port[= ]+[0-9]+' | grep -oE '[0-9]+' || true)"
    ok "Read ${SERVICE_FILE}"
else
    warn "${SERVICE_FILE} not found — falling back to install.sh's defaults."
fi

# An explicit -u always wins; otherwise fall back to install.sh's own derivation.
[[ -n "$U_OPT" ]] && SERVICE_USER="$U_OPT"
SERVICE_USER="${SERVICE_USER:-$DEFAULT_SERVICE_USER}"
APP_DIR="${APP_DIR:-/home/${SERVICE_USER}/freeholdy}"
VENV_DIR="${APP_DIR}/venv"

if ! id "$SERVICE_USER" &>/dev/null; then
    fail "Service user '${SERVICE_USER}' does not exist. Pass -u USER, or run install.sh first."
    exit 1
fi
if [[ ! -f "${APP_DIR}/app/main.py" ]]; then
    fail "No freeholdy checkout at ${APP_DIR} (app/main.py missing)."
    fail "Pass -u USER if the service runs as a different user, or run install.sh first."
    exit 1
fi
if [[ ! -d "${APP_DIR}/.git" ]]; then
    fail "${APP_DIR} is not a git checkout — this install cannot be updated in place."
    fail "Re-install over it with install.sh instead."
    exit 1
fi
# A missing venv is no longer fatal: configure.sh can build one, and step 3a below
# does exactly that before anything needs the interpreter. Only note it here.
VENV_MISSING=0
[[ -x "${VENV_DIR}/bin/python" ]] || VENV_MISSING=1

# The API port only matters for the loopback calls below; nginx fronts the public one.
APP_PORT="${APP_PORT:-$(env_value "${APP_DIR}/.env" PORT)}"
APP_PORT="${APP_PORT:-8000}"
BASE_DOMAIN="$(env_value "${APP_DIR}/.env" BASE_DOMAIN)"
API_URL="http://127.0.0.1:${APP_PORT}"

# Recorded now so the summary can show the transition even after the checkout moves.
OLD_VERSION="$(python3 -c "import json;print(json.load(open('${APP_DIR}/version.json'))['version'])" 2>/dev/null || echo "unknown")"

ok "Service user   : ${SERVICE_USER}"
ok "App directory  : ${APP_DIR}"
ok "API            : ${API_URL}"
ok "Installed ver. : ${OLD_VERSION}"
[[ -n "$BASE_DOMAIN" ]] && ok "Base domain    : ${BASE_DOMAIN}"

# ── 2. Offer versions, ask which one ────────────────────────────────────────────
CURRENT_STEP="fetching available versions"
section "Available versions"

info "Fetching from origin…"
if ! as_user git -C "$APP_DIR" fetch --tags --prune origin 2>&1 | sed 's/^/      /'; then
    fail "git fetch failed — check the network and the origin remote:"
    fail "  sudo -u ${SERVICE_USER} git -C ${APP_DIR} remote -v"
    exit 1
fi
ok "Fetched"

HEAD_SHA="$(as_user git -C "$APP_DIR" rev-parse HEAD)"

# The menu is "main" plus every tag, newest first. A repo with no releases yet
# renders as a single-entry list rather than an error.
REFS=( "$DEFAULT_BRANCH" )
LABELS=()
while IFS= read -r tag; do
    [[ -n "$tag" ]] && REFS+=( "$tag" )
done < <(as_user git -C "$APP_DIR" tag -l --sort=-v:refname)

# Resolve each entry to a commit and to the version.json it declares, so the
# operator picks a version rather than a bare sha.
for i in "${!REFS[@]}"; do
    ref="${REFS[$i]}"
    if [[ "$ref" == "$DEFAULT_BRANCH" ]]; then
        rev="origin/${DEFAULT_BRANCH}"; kind="latest development"
    else
        rev="$ref"; kind="release"
    fi
    sha="$(as_user git -C "$APP_DIR" rev-parse --short "${rev}^{commit}" 2>/dev/null || echo "?")"
    subject="$(as_user git -C "$APP_DIR" log -1 --format='%s' "$rev" 2>/dev/null || echo "")"
    ver="$(as_user git -C "$APP_DIR" show "${rev}:version.json" 2>/dev/null \
           | python3 -c "import json,sys;print(json.load(sys.stdin)['version'])" 2>/dev/null || echo "?")"
    marker=""
    [[ "$(as_user git -C "$APP_DIR" rev-parse "${rev}^{commit}" 2>/dev/null || echo x)" == "$HEAD_SHA" ]] && marker=" ${GREEN}(current)${NC}"
    LABELS+=( "$(printf "%-18s v%-10s %-9s %s%b" "$ref" "$ver" "$sha" "${subject:0:44}" "$marker")" )
done

echo ""
for i in "${!REFS[@]}"; do
    printf "   %b%2d)%b %b\n" "$BOLD" "$((i + 1))" "$NC" "${LABELS[$i]}"
done
[[ "${#REFS[@]}" -eq 1 ]] && { echo ""; info "No release tags published yet — only the ${DEFAULT_BRANCH} branch is available."; }
echo ""

if [[ "$LIST_ONLY" -eq 1 ]]; then
    info "Listing only (-l) — nothing was changed."
    exit 0
fi

TARGET_REF=""
if [[ -n "$V_OPT" ]]; then
    # A revision given on the command line still has to exist.
    for ref in "${REFS[@]}"; do [[ "$ref" == "$V_OPT" ]] && TARGET_REF="$ref"; done
    if [[ -z "$TARGET_REF" ]]; then
        fail "Unknown version '${V_OPT}'. Run with -l to list what is available."
        exit 1
    fi
    info "Version '${TARGET_REF}' selected on the command line"
else
    if [[ ! -r /dev/tty ]]; then
        fail "No terminal attached and no -v REF given — cannot choose a version."
        exit 1
    fi
    while :; do
        printf "  %b?%b  Which version? [1]: " "$CYAN" "$NC" > /dev/tty
        read -r choice < /dev/tty
        choice="${choice:-1}"
        if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#REFS[@]} )); then
            TARGET_REF="${REFS[$((choice - 1))]}"
            break
        fi
        warn "Enter a number between 1 and ${#REFS[@]}."
    done
fi

if [[ "$TARGET_REF" == "$DEFAULT_BRANCH" ]]; then
    TARGET_REV="origin/${DEFAULT_BRANCH}"
else
    TARGET_REV="$TARGET_REF"
fi
TARGET_SHA="$(as_user git -C "$APP_DIR" rev-parse "${TARGET_REV}^{commit}")"
NEW_VERSION="$(as_user git -C "$APP_DIR" show "${TARGET_REV}:version.json" 2>/dev/null \
               | python3 -c "import json,sys;print(json.load(sys.stdin)['version'])" 2>/dev/null || echo "unknown")"
ok "Target: ${TARGET_REF} (v${NEW_VERSION}, ${TARGET_SHA:0:7})"

if [[ "$TARGET_SHA" == "$HEAD_SHA" ]]; then
    warn "This install is already at ${TARGET_REF} (${HEAD_SHA:0:7})."
    warn "Continuing will still rebuild the managed plugins from the current tree."
    confirm "Re-run the update anyway?" || { info "Nothing to do."; exit 0; }
fi

# ── 3. Confirm the plan ─────────────────────────────────────────────────────────
section "Update plan"
echo -e "  Version        : ${CYAN}${OLD_VERSION}${NC} → ${CYAN}${NEW_VERSION}${NC}  (${TARGET_REF})"
echo -e "  Checkout       : ${CYAN}${APP_DIR}${NC} → hard reset to ${TARGET_SHA:0:7}"
echo -e "  Local edits    : ${YELLOW}discarded${NC} (tracked files only — .env, data/ and projects/ are kept)"
echo -e "  Managed plugins: ${CYAN}${MANAGED_PLUGINS[*]}${NC} — removed and rebuilt if installed"
echo -e "  Downtime       : the API and every managed vhost are down until the rebuild finishes"
echo ""
confirm "Proceed with the update?" || { info "Aborted — nothing was changed."; exit 0; }

# ── 3a. Bootstrap a missing venv ────────────────────────────────────────────────
# Everything below needs ${VENV_DIR}/bin/python — the token mint in step 4 first of
# all — so a broken install has to be repaired before we get there. This runs on the
# CURRENT revision's requirements; step 9 re-runs configure.sh after the checkout
# moves and picks up any changes then (its stamp makes that second call free).
# Placed after the confirm above so the "nothing changes until you say yes" promise
# still holds.
if [[ "$VENV_MISSING" -eq 1 ]]; then
    CURRENT_STEP="creating the missing Python venv"
    warn "No virtualenv at ${VENV_DIR} — building one before continuing"
    # The checkout is still on the OLD revision here, which may predate configure.sh.
    if [[ ! -f "${APP_DIR}/configure.sh" ]]; then
        fail "This revision has no configure.sh and there is no venv to work with."
        fail "Build one first, then re-run:  sudo -u ${SERVICE_USER} ${PYTHON_FALLBACK:-python3} -m venv ${VENV_DIR}"
        fail "…or re-run install.sh to repair the install."
        exit 1
    fi
    as_user bash "${APP_DIR}/configure.sh" -d "${APP_DIR}"
    if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
        fail "configure.sh did not produce ${VENV_DIR}/bin/python — cannot continue."
        exit 1
    fi
fi

# ── 4. Temporary API token ──────────────────────────────────────────────────────
# Minted rather than prompted for, so unattended runs work; revoked in the EXIT
# trap so the credential never outlives the script. Existing tokens (including the
# one in the operator's web-UI login link) are untouched.
CURRENT_STEP="minting a temporary API token"
section "Temporary API token"

TOKEN_NAME="update-$(date +%s)"
UPDATE_TOKEN="$(as_user "${VENV_DIR}/bin/python" "${APP_DIR}/scripts/generate_token.py" \
    generate --name "$TOKEN_NAME" 2>/dev/null | grep -oE '[A-Za-z0-9_-]{40,}' | head -n1 || true)"
if [[ -z "$UPDATE_TOKEN" ]]; then
    fail "Could not mint an API token via scripts/generate_token.py."
    exit 1
fi
TOKEN_ID="$(as_user "${VENV_DIR}/bin/python" "${APP_DIR}/scripts/generate_token.py" list 2>/dev/null \
    | awk -v n="$TOKEN_NAME" '$2 == n { print $1 }' | tail -n1 || true)"
[[ -n "$TOKEN_ID" ]] || warn "Minted a token but could not resolve its ID — revoke '${TOKEN_NAME}' by hand afterwards."
ok "Minted '${TOKEN_NAME}' (revoked automatically when this script exits)"

api_get()    { curl -fsS -H "Authorization: Bearer ${UPDATE_TOKEN}" "${API_URL}$1"; }
api_delete() { curl -fsS -X DELETE -H "Authorization: Bearer ${UPDATE_TOKEN}" "${API_URL}$1"; }

# ── 5. Detect which managed plugins are installed ───────────────────────────────
# Nothing on the Project row records which plugin a project came from, so match by
# the conventional project name (the one install.sh and the docs use).
CURRENT_STEP="listing projects"
section "Managed plugins"

if ! PROJECTS_JSON="$(api_get "/projects/" 2>/dev/null)"; then
    fail "The freeholdy API at ${API_URL} did not answer."
    fail "It must be running to tear down the managed plugins (containers, images, nginx vhosts)."
    fail "Start it first:  sudo systemctl start ${SERVICE_NAME}"
    exit 1
fi

INSTALLED=()
declare -A PLUGIN_SSL=()
for p in "${MANAGED_PLUGINS[@]}"; do
    found="$(json_field "[x for x in d if x['name'] == '${p}'][0]['name']" <<<"$PROJECTS_JSON")"
    if [[ -n "$found" ]]; then
        INSTALLED+=( "$p" )
        PLUGIN_SSL["$p"]="$(json_field "([x for x in d if x['name'] == '${p}'][0].get('container') or {}).get('ssl_enabled')" <<<"$PROJECTS_JSON")"
        ok "${p} is installed (ssl=${PLUGIN_SSL[$p]:-false}) — will be rebuilt"
    else
        info "${p} is not installed — will be left alone"
    fi
done
[[ "${#INSTALLED[@]}" -eq 0 ]] && info "Neither managed plugin is installed — only the source will be updated."

# ── 6. Remove the managed plugins ───────────────────────────────────────────────
# A full teardown: containers, images, files, nginx config and the DB row. Safe
# because both projects are regenerated from the repo in step 12; the certs in
# /etc/letsencrypt survive and are reused by the re-add.
CURRENT_STEP="removing the managed plugins"
if [[ "${#INSTALLED[@]}" -gt 0 ]]; then
    section "Removing managed plugins"
    for p in "${INSTALLED[@]}"; do
        info "Deleting project '${p}'…"
        if RESP="$(api_delete "/projects/${p}" 2>/dev/null)"; then
            status="$(json_field "d['status']" <<<"$RESP")"
            if [[ "$status" == "partial" ]]; then
                warn "'${p}' removed, but the teardown reported problems:"
                json_field "'\n'.join('      ' + s for s in d.get('details', []))" <<<"$RESP"
            else
                ok "'${p}' removed"
            fi
        else
            fail "Could not delete project '${p}' via the API."
            exit 1
        fi
    done
fi

# ── 7. Stop the service ─────────────────────────────────────────────────────────
# From here on the box is mid-upgrade, so the trap prints recovery instructions.
CURRENT_STEP="stopping the service"
section "Stopping ${SERVICE_NAME}"
DANGER_ZONE=1

systemctl stop "$SERVICE_NAME"
for _ in $(seq 1 30); do
    systemctl is-active --quiet "$SERVICE_NAME" || break
    sleep 1
done
if systemctl is-active --quiet "$SERVICE_NAME"; then
    fail "${SERVICE_NAME} is still active after 30s — refusing to update the source under a running service."
    exit 1
fi
ok "${SERVICE_NAME} stopped"

# ── 8. Update the git revision ──────────────────────────────────────────────────
CURRENT_STEP="updating the checkout"
section "Updating source to ${TARGET_REF}"

PREV_SHA="$HEAD_SHA"
info "Previous revision: ${PREV_SHA:0:7}"

as_user git -C "$APP_DIR" reset --hard "$TARGET_SHA" | sed 's/^/      /'
# No -x: gitignored paths (.env, data/, projects/, dockerfiles/, compose/,
# nginx_configs/, venv/, cli/.env, *.log) are exactly the runtime state that must
# survive an upgrade. This only removes stale untracked-but-not-ignored files.
as_user git -C "$APP_DIR" clean -fd | sed 's/^/      /'
chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"
ok "Source now at ${TARGET_SHA:0:7} (v${NEW_VERSION})"

# ── 9. Refresh the Python environment ───────────────────────────────────────────
# configure.sh owns the single venv shared by the server and the CLI. Because this
# runs AFTER the reset above, it is the NEW revision's configure.sh and requirements
# that apply — so "reconfigure when requirements.txt changed" falls out for free: the
# script hashes requirements.txt into the venv and no-ops while that hash still holds.
# Unlike the old inline pip call it can also create or repair a venv, which is why it
# is called unconditionally rather than guarded.
CURRENT_STEP="configuring the Python environment"

if [[ -f "${APP_DIR}/configure.sh" ]]; then
    as_user bash "${APP_DIR}/configure.sh" -d "${APP_DIR}"
else
    # Rolling back to a revision from before the venv merge — install the old way so
    # a downgrade still lands a working environment.
    section "Python dependencies"
    info "This revision predates configure.sh — installing dependencies directly…"
    as_user "${VENV_DIR}/bin/pip" install --quiet --upgrade pip
    as_user "${VENV_DIR}/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"
    ok "Dependencies installed"
fi

# New settings all have defaults in app/config.py, so a missing key is never fatal.
# Report them instead of editing the operator's .env behind their back.
if [[ -f "${APP_DIR}/.env.example" && -f "${APP_DIR}/.env" ]]; then
    NEW_KEYS=()
    while IFS= read -r key; do
        grep -qE "^${key}=" "${APP_DIR}/.env" || NEW_KEYS+=( "$key" )
    done < <(grep -oE '^[A-Z_][A-Z0-9_]*=' "${APP_DIR}/.env.example" | tr -d '=')
    if [[ "${#NEW_KEYS[@]}" -gt 0 ]]; then
        warn "This version's .env.example adds settings not present in your .env:"
        for key in "${NEW_KEYS[@]}"; do
            echo -e "        ${CYAN}${key}${NC}  (default: $(env_value "${APP_DIR}/.env.example" "$key"))"
        done
        warn "Defaults from app/config.py apply — add them to ${APP_DIR}/.env only if you want to override."
    fi
fi

# ── 10. Back up the database, then migrate ──────────────────────────────────────
CURRENT_STEP="migrating the database"
section "Database migration"

DB_PATH="${APP_DIR}/data/freeholdy.db"
if [[ -f "$DB_PATH" ]]; then
    DB_BACKUP="${DB_PATH}.bak-$(date +%Y%m%d-%H%M%S)"
    cp -p "$DB_PATH" "$DB_BACKUP"
    chown "$SERVICE_USER":"$SERVICE_USER" "$DB_BACKUP"
    ok "Backed up to ${DB_BACKUP}"
else
    info "No database at ${DB_PATH} yet — nothing to back up"
fi

if [[ -f "${APP_DIR}/migrate_db.sh" ]]; then
    # migrate_db.sh is idempotent and feature-detects every migration inside one
    # transaction, so a no-op upgrade is safe. Unlike install.sh we treat failure
    # as fatal: starting new code against an unmigrated schema is worse than
    # stopping here with the backup in hand.
    if as_user bash "${APP_DIR}/migrate_db.sh" "$DB_PATH" 2>&1 | sed 's/^/      /'; then
        ok "Schema migration complete"
    else
        fail "migrate_db.sh failed — the schema may not match the new code."
        exit 1
    fi
else
    warn "migrate_db.sh not found in ${APP_DIR} — skipping schema migration."
fi

# ── 11. Start the service ───────────────────────────────────────────────────────
CURRENT_STEP="starting the service"
section "Starting ${SERVICE_NAME}"

systemctl start "$SERVICE_NAME"
info "Waiting for ${API_URL}/health…"
HEALTHY=0
for _ in $(seq 1 $((HEALTH_TIMEOUT / 2))); do
    if curl -fsS --max-time 2 "${API_URL}/health" >/dev/null 2>&1; then HEALTHY=1; break; fi
    sleep 2
done
if [[ "$HEALTHY" -ne 1 ]]; then
    fail "${SERVICE_NAME} did not become healthy within ${HEALTH_TIMEOUT}s."
    fail "Check: journalctl -u ${SERVICE_NAME} -e"
    exit 1
fi
ok "${SERVICE_NAME} is running and healthy"
DANGER_ZONE=0     # the service is back up; a later failure is no longer an outage

RUNNING_VERSION="$(curl -fsS "${API_URL}/version" 2>/dev/null | json_field "d['version']" || true)"
[[ -n "$RUNNING_VERSION" ]] && ok "API reports version ${RUNNING_VERSION}"

# ── 12. Reinstall the managed plugins ───────────────────────────────────────────
# Both are non-interactive dockerfile plugins, so POST /add returns 201 immediately
# and the build runs as a background job we poll via GET /projects/{n}/status.
CURRENT_STEP="reinstalling the managed plugins"
REBUILT=(); FAILED=()

wait_for_build() {
    local name="$1" waited=0 status logs
    printf "     "
    while (( waited < BUILD_TIMEOUT )); do
        status="$(api_get "/projects/${name}/status" 2>/dev/null | json_field "d['status']" || true)"
        case "$status" in
            done)              echo ""; return 0 ;;
            error|aborted)
                echo ""
                logs="$(api_get "/projects/${name}/status" 2>/dev/null | json_field "d.get('logs','')" || true)"
                warn "Build of '${name}' finished with status '${status}'. Last lines:"
                echo "$logs" | tail -n 15 | sed 's/^/        /'
                return 1 ;;
            ""|no_job|running) ;;   # still building (or the job registry is not up yet)
            *)                 ;;
        esac
        printf "."
        sleep "$POLL_INTERVAL"
        waited=$(( waited + POLL_INTERVAL ))
    done
    echo ""
    warn "Build of '${name}' did not finish within $(( BUILD_TIMEOUT / 60 )) minutes — it may still be running."
    warn "Follow it with:  curl -H 'Authorization: Bearer <token>' ${API_URL}/projects/${name}/status"
    return 1
}

if [[ "${#INSTALLED[@]}" -gt 0 ]]; then
    section "Reinstalling managed plugins"
    for p in "${INSTALLED[@]}"; do
        info "Reinstalling ${p}…"
        if ! curl -fsS -X POST "${API_URL}/plugins/${p}/add" \
                -H "Authorization: Bearer ${UPDATE_TOKEN}" \
                -H "Content-Type: application/json" \
                -d "{\"project_name\":\"${p}\"}" >/dev/null 2>&1; then
            warn "POST /plugins/${p}/add failed — install it later from the API."
            FAILED+=( "$p" )
            continue
        fi
        [[ "$p" == "webui" ]] && info "Building (this runs npm build — a few minutes)"
        if wait_for_build "$p"; then
            # The job being done is not quite proof the container is up; confirm.
            cstatus="$(api_get "/projects/" 2>/dev/null \
                | json_field "([x for x in d if x['name'] == '${p}'][0].get('container') or {}).get('container_status')" || true)"
            if [[ "$cstatus" == "running" ]]; then
                ok "${p} is running again"
                REBUILT+=( "$p" )
            else
                warn "${p} built, but its container status is '${cstatus:-unknown}'."
                FAILED+=( "$p" )
            fi
        else
            FAILED+=( "$p" )
        fi
    done
fi

# ── Summary ─────────────────────────────────────────────────────────────────────
CURRENT_STEP="summary"
plugin_url() {
    local p="$1" host scheme
    # webui overrides its subdomain via plugin.json's domain_prefix; everything else
    # falls back to the project name (routers/projects.py).
    if [[ "$p" == "webui" ]]; then host="ui.${BASE_DOMAIN}"; else host="${p}.${BASE_DOMAIN}"; fi
    if [[ "${PLUGIN_SSL[$p]:-false}" == "true" ]]; then scheme="https"; else scheme="http"; fi
    echo "${scheme}://${host}"
}

echo ""
echo -e "${BOLD}${GREEN}━━━  freeholdy updated  ━━━${NC}"
echo ""
echo -e "  Version        : ${CYAN}${OLD_VERSION}${NC} → ${CYAN}${NEW_VERSION}${NC}  (${TARGET_REF}, ${TARGET_SHA:0:7})"
echo -e "  App directory  : ${CYAN}${APP_DIR}${NC}"
[[ -n "$DB_BACKUP" ]] && \
echo -e "  DB backup      : ${CYAN}${DB_BACKUP}${NC}"
echo -e "  Service        : ${CYAN}systemctl status ${SERVICE_NAME}${NC}"
echo -e "  Logs           : ${CYAN}journalctl -u ${SERVICE_NAME} -f${NC}"
if [[ "${#REBUILT[@]}" -gt 0 ]]; then
    echo ""
    for p in "${REBUILT[@]}"; do
        printf "  %-14s : %b%s%b\n" "$p" "$CYAN" "$(plugin_url "$p")" "$NC"
    done
    echo -e "  Your existing API tokens still work — no new login link is needed."
fi
if [[ "${#FAILED[@]}" -gt 0 ]]; then
    echo ""
    warn "These plugins did not come back cleanly: ${FAILED[*]}"
    for p in "${FAILED[@]}"; do
        echo -e "      retry with:  ${CYAN}POST /plugins/${p}/add  {\"project_name\":\"${p}\"}${NC}"
    done
fi
if [[ -n "$PREV_SHA" && "$PREV_SHA" != "$TARGET_SHA" ]]; then
    echo ""
    echo -e "  Previous revision was ${CYAN}${PREV_SHA:0:7}${NC} — to go back:"
    echo -e "      ${CYAN}sudo -u ${SERVICE_USER} git -C ${APP_DIR} reset --hard ${PREV_SHA}${NC}"
    echo -e "      ${CYAN}sudo systemctl restart ${SERVICE_NAME}${NC}"
fi
echo ""
