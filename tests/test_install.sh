#!/usr/bin/env bash
#
# test_install.sh — plugin installability smoke test.
#
# Verifies the host prerequisites freeholdy needs to install plugins, then proves
# that each plugin actually builds the way freeholdy installs it:
#
#   • docker is installed and its daemon is reachable
#   • the Compose v2 plugin is present (`docker compose` works) — compose-mode
#     plugins shell out to `docker compose` (app/services/docker_service.py), and
#     this is exactly the check that catches the ws-chat-on-Ubuntu-26.04 regression
#     where `docker.io` shipped without the compose plugin.
#   • every plugin's plugin.json parses and its image(s) build.
#
# The build is run through a faithful copy of freeholdy's provisioning flow rather
# than against the raw plugin dir, because several plugins are not buildable from
# their source tree alone:
#
#   dockerfile plugins (plugins/.../plugin.json deploy_mode=dockerfile)
#     1. stage the Dockerfile into a fresh build context        (plugin_service.stage_dockerfile)
#     2. run the plugin's install.sh (cwd=context, PLUGIN_DIR/PROJECT_DIR set)
#        so it can copy its source in — e.g. webui copies repo webui/ , about
#        copies index.html                                       (docker_service.provision_from_plugin)
#     3. docker build the context
#
#   compose plugins (deploy_mode=compose)
#     1. seed .env (PROJECTS_DIR / DOCKERFILES_DIR)              (plugins.py::_add_compose_plugin)
#     2. stage the whole plugin tree into the project dir        (plugin_service.stage_compose)
#     3. run install.sh "pre" so it can append secrets to .env   (e.g. sftpgo admin creds)
#     4. docker compose config -q  +  docker compose build
#
# Everything is staged under a temp dir and all built images / compose projects
# are cleaned up on exit, so a run leaves no trace.
#
# Usage:
#   ./tests/test_install.sh            # test every plugin under plugins/
#   ./tests/test_install.sh ws-chat    # test a single plugin
#
# Exit code is 0 only when every check passed, so it is CI-usable.
set -euo pipefail

# Repo root is the parent of this script's directory.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGINS_DIR="$ROOT/plugins"

# ── Output helpers (style borrowed from install.sh) ─────────────────────────────
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}✓${NC}  $*"; }
warn() { echo -e "  ${YELLOW}⚠${NC}  $*"; }
info() { echo -e "  ${CYAN}→${NC}  $*"; }
fail() { echo -e "  ${RED}✗${NC}  $*" >&2; }
hr()   { echo -e "${BOLD}── $* ──${NC}"; }

PASS=0
FAIL=0
FAILURES=()
record_pass() { PASS=$((PASS + 1)); }
record_fail() { FAIL=$((FAIL + 1)); FAILURES+=("$1"); }

# Compose manifests recognised by freeholdy (app/routers/projects.py::_COMPOSE_MANIFESTS).
COMPOSE_MANIFESTS=(docker-compose.yml docker-compose.yaml compose.yml compose.yaml)

# Values freeholdy seeds for compose plugins (app/config.py defaults / .env).
PROJECTS_DIR_ABS="$ROOT/projects"
DOCKERFILES_DIR_ABS="$ROOT/dockerfiles"
BASE_DOMAIN="your_domain.com"
if [[ -f "$ROOT/.env" ]]; then
    _bd="$(grep -E '^BASE_DOMAIN=' "$ROOT/.env" | tail -n1 | cut -d= -f2- | tr -d '"'"'"' ' || true)"
    [[ -n "$_bd" ]] && BASE_DOMAIN="$_bd"
fi

# Scratch + cleanup registries.
WORKDIR="$(mktemp -d)"
CLEANUP_IMAGES=()                 # dockerfile image tags
declare -A CLEANUP_COMPOSE=()     # project-name -> staged manifest path

cleanup() {
    hr "cleanup"
    for img in "${CLEANUP_IMAGES[@]:-}"; do
        [[ -z "$img" ]] && continue
        docker rmi -f "$img" &>/dev/null && info "removed image $img" || true
    done
    for proj in "${!CLEANUP_COMPOSE[@]}"; do
        docker compose -p "$proj" -f "${CLEANUP_COMPOSE[$proj]}" down --rmi local &>/dev/null \
            && info "removed compose project $proj" || true
    done
    rm -rf "$WORKDIR"
}
trap cleanup EXIT

# ── Host prerequisite checks ────────────────────────────────────────────────────
check_docker() {
    hr "docker"
    if ! command -v docker &>/dev/null; then
        fail "docker not found on PATH — install it (install.sh provisions docker.io)"
        record_fail "docker: not installed"; return 1
    fi
    ok "docker present ($(command -v docker))"
    if ! docker info &>/dev/null; then
        fail "'docker info' failed — daemon down, or this user can't reach the socket"
        warn "ensure the daemon is running and \$USER is in the 'docker' group (or use sudo)"
        record_fail "docker: daemon unreachable"; return 1
    fi
    ok "docker daemon responding"
    record_pass; return 0
}

check_compose() {
    hr "docker compose"
    if ! docker compose version &>/dev/null; then
        fail "'docker compose' unavailable — the Compose v2 plugin is missing"
        warn "compose-mode plugins (ws-chat, sftpgo) cannot install without it"
        warn "fix: apt-get install -y docker-compose-v2   (install.sh now does this)"
        record_fail "docker compose: plugin missing"; return 1
    fi
    ok "docker compose present ($(docker compose version 2>/dev/null | head -n1))"
    record_pass; return 0
}

# ── Helpers ─────────────────────────────────────────────────────────────────────
# Read a top-level string field out of plugin.json via the json stdlib.
read_manifest_field() {
    python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2],''))" "$1" "$2"
}

find_compose_manifest() {
    local dir="$1"
    for m in "${COMPOSE_MANIFESTS[@]}"; do
        [[ -f "$dir/$m" ]] && { echo "$dir/$m"; return 0; }
    done
    return 1
}

# ── Per-plugin check ────────────────────────────────────────────────────────────
test_dockerfile_plugin() {
    local name="$1" dir="$2" ctx="$WORKDIR/$name"
    if [[ ! -f "$dir/Dockerfile" ]]; then
        fail "deploy_mode=dockerfile but no Dockerfile present"
        record_fail "$name: missing Dockerfile"; return
    fi
    mkdir -p "$ctx"
    cp "$dir/Dockerfile" "$ctx/Dockerfile"                       # stage_dockerfile

    if [[ -f "$dir/install.sh" ]]; then                         # populate the build context
        info "running install.sh (build-context staging)"
        if ! ( cd "$ctx" && PLUGIN_DIR="$dir" PROJECT_DIR="$ctx" BASE_DOMAIN="$BASE_DOMAIN" \
                 bash "$dir/install.sh" ); then
            fail "$name install.sh failed"
            record_fail "$name: install.sh failed"; return
        fi
    fi

    local tag="freeholdy_test_${name}:latest"
    CLEANUP_IMAGES+=("$tag")                                     # register before build
    info "docker build -> $tag"
    if docker build -t "$tag" -f "$ctx/Dockerfile" "$ctx"; then
        ok "$name builds"; record_pass
    else
        fail "$name failed to build"; record_fail "$name: docker build failed"
    fi
}

test_compose_plugin() {
    local name="$1" dir="$2" ctx="$WORKDIR/$name"
    mkdir -p "$ctx"

    # Seed .env, then stage the whole plugin tree (skip plugin.json), like freeholdy.
    printf 'PROJECTS_DIR=%s\nDOCKERFILES_DIR=%s\n' "$PROJECTS_DIR_ABS" "$DOCKERFILES_DIR_ABS" > "$ctx/.env"
    local entry
    for entry in "$dir"/*; do
        [[ "$(basename "$entry")" == "plugin.json" ]] && continue
        cp -r "$entry" "$ctx/"
    done

    if [[ -f "$ctx/install.sh" ]]; then                         # pre phase: append secrets to .env
        info "running install.sh pre (secret/env seeding)"
        # freeholdy runs the pre phase best-effort (plugins.py: subprocess.run(check=False)),
        # so a non-zero exit here is a warning, not a build failure — config/build below
        # still validate the stack (missing env vars just default to blank strings).
        if ! ( cd "$ctx" && PLUGIN_DIR="$dir" PROJECT_DIR="$ctx" PROJECT_NAME="$name" \
                 PROJECTS_DIR="$PROJECTS_DIR_ABS" DOCKERFILES_DIR="$DOCKERFILES_DIR_ABS" \
                 BASE_DOMAIN="$BASE_DOMAIN" bash "$ctx/install.sh" pre ); then
            warn "$name install.sh pre exited non-zero (best-effort; continuing)"
        fi
    fi

    local cmanifest
    if ! cmanifest="$(find_compose_manifest "$ctx")"; then
        fail "deploy_mode=compose but no compose manifest present"
        record_fail "$name: missing compose manifest"; return
    fi
    local proj="fhtest_${name}"
    info "docker compose config"
    if ! docker compose -p "$proj" -f "$cmanifest" config -q; then
        fail "$name compose file is invalid"
        record_fail "$name: compose config invalid"; return
    fi
    info "docker compose build (-p $proj)"
    CLEANUP_COMPOSE["$proj"]="$cmanifest"                        # register before build
    if docker compose -p "$proj" -f "$cmanifest" build; then
        ok "$name builds"; record_pass
    else
        fail "$name failed to build"; record_fail "$name: docker compose build failed"
    fi
}

test_plugin() {
    local name="$1" dir="$PLUGINS_DIR/$1"
    hr "plugin: $name"

    local manifest="$dir/plugin.json"
    if [[ ! -f "$manifest" ]]; then
        fail "no plugin.json in $dir"; record_fail "$name: no plugin.json"; return
    fi
    local pname mode
    if ! pname="$(read_manifest_field "$manifest" name)" \
       || ! mode="$(read_manifest_field "$manifest" deploy_mode)"; then
        fail "plugin.json failed to parse"; record_fail "$name: invalid plugin.json"; return
    fi
    if [[ -z "$pname" || -z "$mode" ]]; then
        fail "plugin.json missing 'name' and/or 'deploy_mode'"
        record_fail "$name: incomplete plugin.json"; return
    fi
    ok "manifest ok (name=$pname, deploy_mode=$mode)"

    case "$mode" in
        dockerfile) test_dockerfile_plugin "$name" "$dir" ;;
        compose)    test_compose_plugin    "$name" "$dir" ;;
        *)          fail "unknown deploy_mode '$mode'"; record_fail "$name: unknown deploy_mode" ;;
    esac
}

# ── Main ────────────────────────────────────────────────────────────────────────
main() {
    echo -e "${BOLD}freeholdy plugin installability test${NC}"
    mkdir -p "$PROJECTS_DIR_ABS" "$DOCKERFILES_DIR_ABS"

    # Prereqs are hard gates — no point building if docker/compose are absent.
    local prereq_ok=1
    check_docker  || prereq_ok=0
    check_compose || prereq_ok=0
    if [[ "$prereq_ok" -ne 1 ]]; then
        fail "host prerequisites not met — skipping plugin builds"
    else
        local plugins=()
        if [[ $# -ge 1 ]]; then
            if [[ ! -d "$PLUGINS_DIR/$1" ]]; then
                fail "no such plugin: $1 (looked in $PLUGINS_DIR)"; exit 2
            fi
            plugins=("$1")
        else
            for d in "$PLUGINS_DIR"/*/; do plugins+=("$(basename "$d")"); done
        fi
        for p in "${plugins[@]}"; do test_plugin "$p"; done
    fi

    echo
    hr "summary"
    local total=$((PASS + FAIL))
    if [[ "$FAIL" -eq 0 ]]; then
        ok "all checks passed ($PASS/$total)"; exit 0
    fi
    fail "$FAIL of $total checks failed:"
    for f in "${FAILURES[@]}"; do echo -e "      ${RED}-${NC} $f"; done
    exit 1
}

main "$@"
