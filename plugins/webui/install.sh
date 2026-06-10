#!/bin/bash
# install.sh — pre-build staging for the webui server plugin.
#
# freeholdy invokes this before `docker build` with:
#   cwd         = PROJECT_DIR (the build context)
#   PLUGIN_DIR  = absolute path to plugins/webui/
#   PROJECT_DIR = absolute path to the dockerfiles/{project_name}/ build context
#
# This script copies the webui source tree into the build context and writes
# a .env file so Vite bakes the correct API URL into the JS bundle at build time.

set -euo pipefail

# ── Locate repo root and webui source ────────────────────────────────────────
PM_ROOT="$(cd "${PLUGIN_DIR}/../.." && pwd)"
WEBUI_SRC="${PM_ROOT}/webui"

if [[ ! -d "$WEBUI_SRC" ]]; then
    echo "webui: cannot find webui/ at ${WEBUI_SRC}" >&2
    exit 1
fi

# ── Read BASE_DOMAIN from freeholdy .env ───────────────────────────────────
BASE_DOMAIN="your_domain.com"
if [[ -f "${PM_ROOT}/.env" ]]; then
    _raw="$(grep -E '^BASE_DOMAIN=' "${PM_ROOT}/.env" | head -1 || true)"
    if [[ -n "$_raw" ]]; then
        _domain="${_raw#BASE_DOMAIN=}"
        _domain="${_domain//\"/}"
        _domain="${_domain// /}"
        [[ -n "$_domain" ]] && BASE_DOMAIN="$_domain"
    fi
fi
API_URL="https://api.${BASE_DOMAIN}"

# ── Copy webui source into the build context ──────────────────────────────────
echo "webui: copying source from ${WEBUI_SRC}…"
cp -r "${WEBUI_SRC}/." "${PROJECT_DIR}/"

# ── Write .env so Vite picks up VITE_API_URL during npm run build ─────────────
echo "VITE_API_URL=${API_URL}" > "${PROJECT_DIR}/.env"
echo "webui: VITE_API_URL=${API_URL}"
