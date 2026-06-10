#!/bin/bash
# install.sh — sftp-go plugin setup (user plugin, interactive).
#
# Two-phase compose contract:
#
#   Phase "pre"  (interactive, runs attached to a WebSocket/pty before docker compose up):
#     Prompts for an admin username, admin password, a storage folder, and the public SFTP
#     port, then persists them to ${PROJECT_DIR}/.env for docker compose substitution.
#
#   Phase "post" (background thread, after docker compose up — NOT interactive):
#     Waits for the REST API, creates a matching SFTPGo file user (home /srv/data), and
#     writes credentials to ${PROJECT_DIR}/CREDENTIALS.txt (mode 600).
#
# Environment variables provided by freeholdy:
#   PLUGIN_DIR                 — this plugin's source directory
#   PROJECT_DIR                — compose project directory (contains .env, compose files)
#   PROJECT_NAME               — project name
#   PROJECTS_DIR               — absolute path to freeholdy's unified projects/ directory
#   BASE_DOMAIN                — e.g. "your_domain.com"
#   SERVICE_SFTPGO_LOCAL_PORT  — loopback port allocated for nginx → WebClient (post phase)

set -euo pipefail

PHASE="${1:-pre}"

_gen_pass() {
    tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24
}

# Idempotently set KEY=VALUE in ${PROJECT_DIR}/.env (drop any prior line first).
_set_env() {
    local key="$1" value="$2" env_file="${PROJECT_DIR}/.env"
    touch "$env_file"
    grep -v "^${key}=" "$env_file" > "${env_file}.tmp" || true
    mv "${env_file}.tmp" "$env_file"
    printf '%s=%s\n' "$key" "$value" >> "$env_file"
}

# ── Pre phase (interactive) ──────────────────────────────────────────────────────
if [[ "$PHASE" == "pre" ]]; then
    # Admin username.
    read -r -p "Admin username [admin]: " ADMIN_USER
    ADMIN_USER="${ADMIN_USER:-admin}"

    # Admin password (hidden; blank → auto-generate).
    while true; do
        read -r -s -p "Admin password (blank = auto-generate): " ADMIN_PASS; echo
        if [[ -z "$ADMIN_PASS" ]]; then
            ADMIN_PASS="$(_gen_pass)"
            echo "Generated admin password: ${ADMIN_PASS}"
            break
        fi
        read -r -s -p "Confirm password: " ADMIN_PASS2; echo
        if [[ "$ADMIN_PASS" == "$ADMIN_PASS2" ]]; then
            break
        fi
        echo "Passwords did not match — try again."
    done

    # Storage folder (required, absolute, created if missing).
    while true; do
        read -r -p "Folder to store files (absolute path): " DATA_DIR
        if [[ -z "$DATA_DIR" ]]; then
            echo "A storage folder is required."
            continue
        fi
        if [[ "$DATA_DIR" != /* ]]; then
            echo "Path must be absolute (start with /)."
            continue
        fi
        if mkdir -p "$DATA_DIR" 2>/dev/null; then
            break
        fi
        echo "Could not create '${DATA_DIR}' — pick another path."
    done

    # Public SFTP port (numeric 1024-65535, not already listening).
    while true; do
        read -r -p "Public SFTP port [2022]: " SFTP_PORT
        SFTP_PORT="${SFTP_PORT:-2022}"
        if ! [[ "$SFTP_PORT" =~ ^[0-9]+$ ]] || (( SFTP_PORT < 1024 || SFTP_PORT > 65535 )); then
            echo "Enter an integer between 1024 and 65535."
            continue
        fi
        if ss -ltn 2>/dev/null | grep -q ":${SFTP_PORT}\b"; then
            echo "Port ${SFTP_PORT} is already in use — pick another."
            continue
        fi
        break
    done

    _set_env SFTPGO_ADMIN_USER "$ADMIN_USER"
    _set_env SFTPGO_ADMIN_PASS "$ADMIN_PASS"
    _set_env DATA_DIR "$DATA_DIR"
    _set_env SFTP_PORT "$SFTP_PORT"

    echo ""
    echo "sftp-go configured:"
    echo "  Admin user: ${ADMIN_USER}"
    echo "  Password:   ********"
    echo "  Folder:     ${DATA_DIR}"
    echo "  SFTP port:  ${SFTP_PORT}"
    exit 0
fi

# ── Post phase (background, non-interactive) ─────────────────────────────────────

# Load the values the pre phase wrote.
# shellcheck source=/dev/null
set -a; source "${PROJECT_DIR}/.env"; set +a

LOCAL_PORT="${SERVICE_SFTPGO_LOCAL_PORT}"
API="http://127.0.0.1:${LOCAL_PORT}/api/v2"
SFTP_USER="${SFTPGO_ADMIN_USER}"
CREDS_FILE="${PROJECT_DIR}/CREDENTIALS.txt"

# Wait for REST API (up to 120 s — image pull + container start can be slow).
echo "sftp-go: waiting for REST API on port ${LOCAL_PORT}…"
MAX_WAIT=120
WAITED=0
until curl -sf "${API}/version" >/dev/null 2>&1; do
    if [[ $WAITED -ge $MAX_WAIT ]]; then
        echo "sftp-go: API did not become ready after ${MAX_WAIT}s. Check: docker logs freeholdy_${PROJECT_NAME}_sftpgo" >&2
        exit 1
    fi
    sleep 2
    WAITED=$(( WAITED + 2 ))
done
echo "sftp-go: REST API ready after ${WAITED}s"

# Obtain JWT with the admin credentials.
TOKEN_RESPONSE=$(curl -sf -X GET "${API}/token" \
    -u "${SFTPGO_ADMIN_USER}:${SFTPGO_ADMIN_PASS}" \
    -H "Content-Type: application/json")
JWT=$(echo "$TOKEN_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
if [[ -z "$JWT" ]]; then
    echo "sftp-go: failed to obtain JWT — check admin credentials in ${PROJECT_DIR}/.env" >&2
    exit 1
fi

AUTH="Authorization: Bearer ${JWT}"

# Create the file user (same creds as admin, home /srv/data) — skip if it already exists.
HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "$AUTH" "${API}/users/${SFTP_USER}")

if [[ "$HTTP_STATUS" == "200" ]]; then
    echo "sftp-go: user '${SFTP_USER}' already exists — skipping creation"
else
    USER_PAYLOAD=$(SFTP_USER="$SFTP_USER" SFTPGO_ADMIN_PASS="$SFTPGO_ADMIN_PASS" python3 -c "
import json, os
print(json.dumps({
    'username':    os.environ['SFTP_USER'],
    'password':    os.environ['SFTPGO_ADMIN_PASS'],
    'home_dir':    '/srv/data',
    'status':      1,
    'permissions': {'/': ['*']}
}))
")
    curl -sf -X POST "${API}/users" \
        -H "$AUTH" \
        -H "Content-Type: application/json" \
        -d "$USER_PAYLOAD" >/dev/null
    echo "sftp-go: user '${SFTP_USER}' created with home /srv/data"
fi

# Save credentials alongside the project (per-instance, not a global file).
cat > "$CREDS_FILE" << CREDS
# sftp-go credentials — generated by the sftp-go plugin install.sh
# Keep this file secret.

ADMIN_USER=${SFTPGO_ADMIN_USER}
ADMIN_PASS=${SFTPGO_ADMIN_PASS}

SFTP_HOST=${BASE_DOMAIN}
SFTP_PORT=${SFTP_PORT}
SFTP_USER=${SFTP_USER}
SFTP_PASS=${SFTPGO_ADMIN_PASS}
CREDS
chmod 600 "$CREDS_FILE"
echo "sftp-go: credentials saved to ${CREDS_FILE}"

echo ""
echo "━━━  sftp-go setup complete  ━━━"
echo "  Admin:     https://sftpgo.${PROJECT_NAME}.${BASE_DOMAIN}/web/admin   (user: ${SFTPGO_ADMIN_USER})"
echo "  WebClient: https://sftpgo.${PROJECT_NAME}.${BASE_DOMAIN}/web/client  (user: ${SFTP_USER})"
echo "  SFTP:      ${BASE_DOMAIN}:${SFTP_PORT}  (user: ${SFTP_USER})"
echo "  Files dir: ${DATA_DIR}  →  /srv/data"
echo "  Credentials: ${CREDS_FILE}"
