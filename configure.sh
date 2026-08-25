#!/bin/bash
# =============================================================================
# configure.sh
# Create (or refresh) the ONE Python virtualenv this project uses.
#
# freeholdy used to carry two venvs: the server's at <repo>/venv and a second one
# under cli/ for fhcli. They have been merged — cli/fhcli.py re-execs itself under
# <repo>/venv/bin/python, so a single environment serves both. This script owns
# that venv, and is the only place that knows how to build it.
#
# It runs as the CURRENT user — there is no -u/service-user flag. On a server that
# means install.sh/update.sh invoke it through their own `as_user`, exactly the way
# they already invoke migrate_db.sh; on a workstation you just run it yourself. The
# only step that needs root is the python3.X-venv apt fallback, which calls sudo
# directly (install.sh grants the service account passwordless sudo).
#
# Re-running is cheap: a hash of requirements.txt is stored inside the venv, and the
# pip work is skipped while it still matches. That is what lets update.sh call this
# unconditionally and still only do work when the dependencies actually changed.
#
# Usage:
#   bash configure.sh [-d DIR] [-f]
#     -d DIR   project dir holding requirements.txt (default: this script's own dir)
#     -f       force reinstall even when the stamp matches
# =============================================================================

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
# Acceptable CPython minor versions for freeholdy's venv (inclusive range), kept in
# sync with install.sh:
# - MIN: what the application code needs (FastAPI/Pydantic features).
# - MAX: newest interpreter for which our deps (notably pydantic-core) ship
#   prebuilt wheels. Source-building on newer Python pulls in Rust + a matching
#   PyO3, which broke installs on 3.14. Bump MAX when upstream catches up.
PYTHON_MIN_MINOR=11
PYTHON_MAX_MINOR=14

STAMP_NAME=".freeholdy-requirements"   # lives INSIDE the venv — see below

# ── Colours / helpers ──────────────────────────────────────────────────────────
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
ok()      { echo -e "  ${GREEN}✓${NC}  $*"; }
warn()    { echo -e "  ${YELLOW}⚠${NC}  $*"; }
info()    { echo -e "  ${CYAN}→${NC}  $*"; }
fail()    { echo -e "  ${RED}✗${NC}  $*" >&2; }
section() { echo -e "\n${BOLD}━━━  $*  ━━━${NC}"; }

# Highest-minor python3.X on PATH within [MIN, MAX]. Echoes the binary or fails.
# install.sh has the same function but never exports PYTHON_BIN, so we resolve our
# own — that is what makes this script runnable on its own.
resolve_python_bin() {
    local minor bin
    for minor in $(seq "$PYTHON_MAX_MINOR" -1 "$PYTHON_MIN_MINOR"); do
        bin="python3.${minor}"
        if command -v "$bin" &>/dev/null; then echo "$bin"; return 0; fi
    done
    return 1
}

# sha256 of a file, bare hash only. Used for the requirements stamp.
file_hash() { sha256sum "$1" | cut -d' ' -f1; }

# ── Argument parsing ────────────────────────────────────────────────────────────
FORCE=0
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"

while getopts ":d:fh" opt; do
    case "$opt" in
        d) PROJECT_DIR="$OPTARG" ;;
        f) FORCE=1 ;;
        h) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        \?) fail "Unknown option: -$OPTARG"; exit 1 ;;
        :)  fail "Option -$OPTARG requires an argument"; exit 1 ;;
    esac
done

PROJECT_DIR="$(cd "$PROJECT_DIR" 2>/dev/null && pwd || echo "$PROJECT_DIR")"
VENV_DIR="${PROJECT_DIR}/venv"
REQUIREMENTS="${PROJECT_DIR}/requirements.txt"
STAMP="${VENV_DIR}/${STAMP_NAME}"

section "Python environment"

if [[ ! -f "$REQUIREMENTS" ]]; then
    fail "No requirements.txt at ${REQUIREMENTS}"
    fail "Pass -d DIR pointing at the freeholdy checkout."
    exit 1
fi

# ── 1. Resolve the interpreter ──────────────────────────────────────────────────
if ! PYTHON_BIN="$(resolve_python_bin)"; then
    fail "No Python 3.${PYTHON_MIN_MINOR}–3.${PYTHON_MAX_MINOR} found on PATH."
    fail "Install one (e.g. 'sudo apt-get install python3.13 python3.13-venv') and retry."
    exit 1
fi
ok "Interpreter: ${PYTHON_BIN}  ($(command -v "$PYTHON_BIN"))"

# A deadsnakes python3.X without its matching -venv package fails with
# "ensurepip is not available". Fix it when we can do so without prompting;
# a workstation without passwordless sudo just gets told what to install.
if ! "$PYTHON_BIN" -c "import ensurepip" &>/dev/null; then
    if command -v sudo &>/dev/null && sudo -n true 2>/dev/null; then
        info "ensurepip missing — installing ${PYTHON_BIN}-venv…"
        sudo apt-get install -y "${PYTHON_BIN}-venv" \
            || warn "Could not install ${PYTHON_BIN}-venv — venv creation may fail below."
    else
        warn "ensurepip is missing for ${PYTHON_BIN} and passwordless sudo is unavailable."
        warn "Install it yourself if the next step fails:  sudo apt-get install ${PYTHON_BIN}-venv"
    fi
fi

# ── 2. Create or recreate the venv ──────────────────────────────────────────────
# Recreating on an interpreter change is deliberate: a venv built against a Python
# that is no longer on PATH silently breaks every entry point in it.
if [[ ! -d "$VENV_DIR" ]]; then
    info "Creating venv with ${PYTHON_BIN}…"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    ok "venv created at ${VENV_DIR}"
else
    VENV_PY_VER=$("${VENV_DIR}/bin/python" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "?")
    EXPECTED_VER=$("$PYTHON_BIN" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    if [[ "$VENV_PY_VER" != "$EXPECTED_VER" ]]; then
        warn "Existing venv uses Python ${VENV_PY_VER}, expected ${EXPECTED_VER} — recreating"
        rm -rf "$VENV_DIR"
        "$PYTHON_BIN" -m venv "$VENV_DIR"
        ok "venv recreated at ${VENV_DIR} with ${PYTHON_BIN}"
    else
        ok "venv present at ${VENV_DIR} (Python ${VENV_PY_VER})"
    fi
fi

# ── 3. Install dependencies, unless nothing changed ─────────────────────────────
# The stamp lives inside the venv on purpose: recreating the venv above throws it
# away, so a rebuilt environment always reinstalls without any extra bookkeeping.
WANT_HASH="$(file_hash "$REQUIREMENTS")"
HAVE_HASH="$(cat "$STAMP" 2>/dev/null || true)"

if [[ "$FORCE" -eq 0 && "$WANT_HASH" == "$HAVE_HASH" ]]; then
    ok "requirements.txt unchanged — dependencies already current"
else
    if [[ "$FORCE" -eq 1 ]]; then
        info "Forced reinstall (-f) — installing dependencies…"
    elif [[ -z "$HAVE_HASH" ]]; then
        info "Installing dependencies…"
    else
        info "requirements.txt changed since last configure — installing…"
    fi
    "${VENV_DIR}/bin/pip" install --quiet --upgrade pip
    "${VENV_DIR}/bin/pip" install --quiet -r "$REQUIREMENTS"
    echo "$WANT_HASH" > "$STAMP"
    ok "Dependencies installed ($(grep -cE '^[^#[:space:]]' "$REQUIREMENTS") packages pinned)"
fi

# ── 4. Retire the old CLI venv ──────────────────────────────────────────────────
# cli/ used to carry its own venv. It is gitignored, so update.sh's `git clean -fd`
# will never remove it — this is the only thing that does.
if [[ -d "${PROJECT_DIR}/cli/venv" ]]; then
    rm -rf "${PROJECT_DIR}/cli/venv"
    info "Removed the obsolete cli/venv — fhcli now runs from ${VENV_DIR}"
fi

ok "Python environment ready"
