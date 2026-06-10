#!/bin/bash
# =============================================================================
# install.sh — local Docker install for the freeholdy Web UI
#
# Builds the React UI from GitHub and runs it in Docker — no git clone needed.
# Designed to be piped from curl on any machine that has Docker installed.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/aafanasev-dev/freeholdy/main/webui/install.sh | bash
#
# Options (env vars):
#   FREEHOLDY_API      — freeholdy API URL  (prompted if not set)
#   FREEHOLDY_UI_PORT  — host port for the UI (default: 3000)
#
# Example with custom API:
#   FREEHOLDY_API=https://api.example.com bash <(curl -fsSL <url>)
# =============================================================================

set -euo pipefail

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}✓${NC}  $*"; }
info() { echo -e "  ${CYAN}→${NC}  $*"; }
warn() { echo -e "  ${YELLOW}⚠${NC}  $*"; }
fail() { echo -e "  ${RED}✗${NC}  $*" >&2; exit 1; }

REPO="github.com/aafanasev-dev/freeholdy"
IMAGE="freeholdy-webui:local"
CONTAINER="freeholdy_webui"
PORT="${FREEHOLDY_UI_PORT:-3000}"
API_URL="${FREEHOLDY_API:-}"

echo -e "\n${BOLD}━━━  freeholdy Web UI — local install  ━━━${NC}\n"

# ── Docker check ──────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    fail "docker not found. Install Docker first: https://docs.docker.com/get-docker/"
fi
if ! docker info &>/dev/null; then
    fail "Docker daemon is not running. Start it and retry."
fi
ok "Docker is available"

# ── API URL ───────────────────────────────────────────────────────────────────
if [[ -z "$API_URL" ]]; then
    read -rp "  freeholdy API URL [https://api.your_domain.com]: " API_URL
    API_URL="${API_URL:-https://api.your_domain.com}"
fi
ok "API URL: ${API_URL}"

# ── Build image from GitHub ───────────────────────────────────────────────────
info "Building image from ${REPO} (this may take a minute)…"
docker build \
    --build-arg "VITE_API_URL=${API_URL}" \
    -t "$IMAGE" \
    "https://${REPO}.git#main:webui"
ok "Image built: ${IMAGE}"

# ── Run container ─────────────────────────────────────────────────────────────
docker rm -f "$CONTAINER" 2>/dev/null || true
docker run -d \
    --name "$CONTAINER" \
    --restart unless-stopped \
    -p "${PORT}:14173" \
    "$IMAGE"
ok "Container '${CONTAINER}' started"

# ── CORS reminder ──────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}━━━  Done  ━━━${NC}"
echo ""
echo -e "  Web UI:  ${CYAN}http://localhost:${PORT}${NC}"
echo -e "  API:     ${CYAN}${API_URL}${NC}"
echo ""
warn "CORS: add 'http://localhost:${PORT}' to CORS_ORIGINS in your freeholdy .env"
warn "      (it's already in the default list if you're on port 3000)"
echo ""
echo -e "  Useful commands:"
echo -e "    ${CYAN}docker logs ${CONTAINER} -f${NC}   # live logs"
echo -e "    ${CYAN}docker stop ${CONTAINER}${NC}       # stop"
echo -e "    ${CYAN}docker rm -f ${CONTAINER}${NC}      # remove"
echo ""
