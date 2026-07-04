#!/bin/bash
# install.sh — Outline Server (Shadowbox) plugin setup.
#
# Interactive compose plugin (plugin.json "interactive": true): the pre phase runs
# attached to a WebSocket session (pty) so it can prompt the user and, crucially,
# print the Outline Manager connection string live (the post phase output is NOT
# streamed anywhere the user can see it).
#
#   Phase "pre"  (interactive, before docker compose up):
#     Confirms the public host, picks the management-API port and the Shadowsocks
#     access-key port, generates the self-signed TLS cert + secret API URL prefix
#     into ./persisted-state (keep-if-present — the operator's saved Outline
#     Manager connection is pinned to this exact cert fingerprint + prefix + host),
#     writes them to ./.env for compose substitution, then prints and saves the
#     {"apiUrl":…,"certSha256":…} JSON the Outline Manager desktop app imports.
#
#   Phase "post" (background thread, after docker compose up, NOT streamed):
#     Waits for the container + management API to come up, pins the port used for
#     new access keys to KEYS_PORT (so there is a single predictable data port to
#     open in the firewall), sets the server name, chowns ./persisted-state back to
#     the project owner (Shadowbox writes its config + keys as root — DELETE's
#     rmtree must be able to remove them), verifies the ports are bound, and writes
#     a mode-600 outline-info summary.
#
#   Both phases are idempotent: this stack publishes no TCP `ports:` (host
#   networking), so freeholdy creates no ComposeService rows and reconnecting to
#   the install WebSocket re-runs pre against the live stack — existing .env values
#   become the defaults and the cert/prefix are kept, never rotated.
#
# This runs on HOST networking (like Outline's own `docker run --net host`) — there
# is no subdomain, no nginx vhost, no Let's Encrypt cert. The operator must open
# BOTH the management-API port (tcp) and the access-key port (tcp+udp) in the cloud
# firewall. The freeholdy service user never touches host sysctl/iptables.
#
# Environment variables provided by freeholdy:
#   PLUGIN_DIR    — this plugin's source directory
#   PROJECT_DIR   — compose project directory (contains .env, compose files)
#   PROJECT_NAME  — project name (e.g. "vpn")
#   BASE_DOMAIN   — e.g. "your_domain.com"
#   (no SERVICE_*_LOCAL_PORT — nothing is exposed through nginx)

set -euo pipefail

PHASE="${1:-post}"

CONTAINER="freeholdy_${PROJECT_NAME}_shadowbox"
STATE_DIR="${PROJECT_DIR}/persisted-state"
CERT_FILE="${STATE_DIR}/shadowbox-selfsigned.crt"
KEY_FILE="${STATE_DIR}/shadowbox-selfsigned.key"

# Idempotent KEY=VALUE write to the project .env (drop any previous line first).
# Values are single-quoted so a later `source` is safe even with metacharacters.
_env_set() {
    local key="$1" value="$2"
    grep -v "^${key}=" "${PROJECT_DIR}/.env" > "${PROJECT_DIR}/.env.tmp" 2>/dev/null || true
    mv "${PROJECT_DIR}/.env.tmp" "${PROJECT_DIR}/.env"
    printf "%s='%s'\n" "$key" "${value//\'/\'\\\'\'}" >> "${PROJECT_DIR}/.env"
}

# Keep-if-present variant: never rotate a value a previous run already wrote
# (rotating the cert/prefix/ports would break the saved Outline Manager access).
_env_keep() {
    local key="$1"
    grep -q "^${key}=" "${PROJECT_DIR}/.env" 2>/dev/null || _env_set "$key" "$2"
}

# Is our shadowbox running? On host networking it legitimately holds the API/keys
# ports, so a re-run must treat those prior ports as free.
_running() {
    [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)" == "true" ]]
}

# Port free to bind? A candidate equal to a prior value while our own container is
# running counts as free (that's us holding it).
_tcp_port_free() {  # _tcp_port_free PORT PRIOR
    local port="$1" prior="${2:-}"
    if _running && [[ -n "$prior" && "$port" == "$prior" ]]; then return 0; fi
    ss -Htln 2>/dev/null | grep -qE "[:.]${port}([[:space:]]|$)" && return 1
    return 0
}
_udp_port_free() {  # _udp_port_free PORT PRIOR
    local port="$1" prior="${2:-}"
    if _running && [[ -n "$prior" && "$port" == "$prior" ]]; then return 0; fi
    ss -Huln 2>/dev/null | grep -qE "[:.]${port}([[:space:]]|$)" && return 1
    return 0
}

# Random unprivileged high port in [20000, 60000] via $RANDOM (no urandom pipes —
# under pipefail a `tr | head` chain dies with SIGPIPE and `set -e` kills install).
_rand_port() { echo $(( 20000 + RANDOM % 40001 )); }

# ── Pre phase ──────────────────────────────────────────────────────────────────
if [[ "$PHASE" == "pre" ]]; then
    echo "Outline Server (Shadowbox) setup — self-hosted Shadowsocks VPN on host networking."
    echo "No subdomain, no SSL vhost — nginx is not involved at all."
    echo ""

    if _running; then
        echo "NOTE: ${CONTAINER} is already running — this re-run keeps the existing"
        echo "cert, API prefix and ports; existing settings are the defaults."
        echo ""
    fi

    # Load prior answers (if any) as prompt defaults.
    # shellcheck source=/dev/null
    set -a; source "${PROJECT_DIR}/.env" 2>/dev/null || true; set +a
    PRIOR_HOST="${OUTLINE_HOST:-}"
    PRIOR_API="${SB_API_PORT:-}"
    PRIOR_KEYS="${KEYS_PORT:-}"

    mkdir -p "$STATE_DIR"
    chmod 700 "$STATE_DIR"

    # Public host the Outline Manager and clients connect to. Try DNS on the base
    # domain first (it should already point at this VPS), then an external echo.
    HOST="${PRIOR_HOST:-}"
    [[ -z "$HOST" ]] && HOST=$(dig +short A "$BASE_DOMAIN" 2>/dev/null | head -n1 || true)
    [[ -z "$HOST" ]] && HOST=$(getent ahostsv4 "$BASE_DOMAIN" 2>/dev/null | awk 'NR==1{print $1}' || true)
    [[ -z "$HOST" ]] && HOST=$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || true)
    while :; do
        read -r -p "Public host (IP or hostname) clients connect to [${HOST:-required}]: " HOST_IN
        HOST="${HOST_IN:-$HOST}"
        [[ -n "$HOST" ]] && break
        echo "A public host is required — it goes into the management API URL."
    done

    # Management-API port (TCP).
    DEF_API="${PRIOR_API:-$(_rand_port)}"
    _tcp_port_free "$DEF_API" "$PRIOR_API" || DEF_API=$(_rand_port)
    while :; do
        read -r -p "Management API port (tcp) [${DEF_API}]: " API_IN
        SB_API_PORT="${API_IN:-$DEF_API}"
        if ! [[ "$SB_API_PORT" =~ ^[0-9]+$ ]] || (( SB_API_PORT < 1024 || SB_API_PORT > 65535 )); then
            echo "'${SB_API_PORT}' is not a valid port — use a number between 1024 and 65535."
            continue
        fi
        if ! _tcp_port_free "$SB_API_PORT" "$PRIOR_API"; then
            echo "TCP ${SB_API_PORT} is already in use on this host — pick another."
            continue
        fi
        break
    done

    # Shadowsocks access-key port (TCP + UDP). Clients connect here.
    DEF_KEYS="${PRIOR_KEYS:-$(_rand_port)}"
    { _tcp_port_free "$DEF_KEYS" "$PRIOR_KEYS" && _udp_port_free "$DEF_KEYS" "$PRIOR_KEYS"; } || DEF_KEYS=$(_rand_port)
    while :; do
        read -r -p "Access-key port (tcp+udp, clients connect here) [${DEF_KEYS}]: " KEYS_IN
        KEYS_PORT="${KEYS_IN:-$DEF_KEYS}"
        if ! [[ "$KEYS_PORT" =~ ^[0-9]+$ ]] || (( KEYS_PORT < 1024 || KEYS_PORT > 65535 )); then
            echo "'${KEYS_PORT}' is not a valid port — use a number between 1024 and 65535."
            continue
        fi
        if [[ "$KEYS_PORT" == "$SB_API_PORT" ]]; then
            echo "The access-key port must differ from the management API port (${SB_API_PORT})."
            continue
        fi
        if ! _tcp_port_free "$KEYS_PORT" "$PRIOR_KEYS" || ! _udp_port_free "$KEYS_PORT" "$PRIOR_KEYS"; then
            echo "Port ${KEYS_PORT} is already in use (tcp or udp) on this host — pick another."
            continue
        fi
        break
    done

    # Secret management-API URL prefix (128-bit, URL-safe base64). openssl rand
    # avoids a urandom-into-head pipe (SIGPIPE-safe under pipefail). Keep-if-present.
    _env_keep SB_API_PREFIX "$(openssl rand -base64 16 | tr '/+' '_-' | tr -d '=')"

    # Self-signed cert keyed to the host (mirrors Outline's install_server.sh:
    # 4096-bit RSA, ~100y). Generated once and kept — the Outline Manager pins its
    # SHA-256 fingerprint, so rotating it would orphan the saved connection.
    if [[ ! -f "$CERT_FILE" || ! -f "$KEY_FILE" ]]; then
        echo "Generating self-signed TLS certificate for the management API…"
        openssl req -x509 -nodes -days 36500 -newkey rsa:4096 \
            -subj "/CN=${HOST}" \
            -keyout "$KEY_FILE" -out "$CERT_FILE" >/dev/null 2>&1
        chmod 600 "$KEY_FILE"
        chmod 644 "$CERT_FILE"
    fi
    CERT_SHA256=$(openssl x509 -in "$CERT_FILE" -noout -sha256 -fingerprint | sed 's/.*=//;s/://g')

    _env_set OUTLINE_HOST "$HOST"
    _env_set SB_API_PORT "$SB_API_PORT"
    _env_set KEYS_PORT "$KEYS_PORT"
    _env_keep SB_SERVER_NAME "$PROJECT_NAME"

    # Re-load: SB_API_PREFIX / SB_SERVER_NAME may be older keep-if-present values.
    # shellcheck source=/dev/null
    set -a; source "${PROJECT_DIR}/.env"; set +a

    API_URL="https://${HOST}:${SB_API_PORT}/${SB_API_PREFIX}"
    ACCESS_JSON="{\"apiUrl\":\"${API_URL}\",\"certSha256\":\"${CERT_SHA256}\"}"

    # Save the Outline Manager import string (mode 600) and print it live.
    printf '%s\n' "$ACCESS_JSON" > "${PROJECT_DIR}/outline-access.txt"
    chmod 600 "${PROJECT_DIR}/outline-access.txt"

    if _running && [[ -n "$PRIOR_HOST" && "$PRIOR_HOST" != "$HOST" ]]; then
        echo ""
        echo "WARNING: the host changed (${PRIOR_HOST} → ${HOST}). The cert fingerprint is"
        echo "unchanged, but re-import the config string below into the Outline Manager."
    fi

    echo ""
    echo "━━━  Outline Manager connection string  ━━━"
    echo "Copy the line below and paste it into the Outline Manager desktop app"
    echo "(\"Add server\" → \"already have a server\"):"
    echo ""
    echo "  ${ACCESS_JSON}"
    echo ""
    echo "Also saved to: ${PROJECT_DIR}/outline-access.txt"
    echo ""
    echo "IMPORTANT — open BOTH ports in your cloud provider security group / firewall:"
    echo "  • ${SB_API_PORT}/tcp        (management API — the Outline Manager connects here)"
    echo "  • ${KEYS_PORT}/tcp and /udp (access keys — VPN clients connect here)"
    echo "Host-bound ports bypass ufw on stock installs, but if you filter the"
    echo "DOCKER-USER chain add: ufw allow ${SB_API_PORT}/tcp; ufw allow ${KEYS_PORT}"
    echo ""
    echo "The container starts next; keys created in the Outline Manager will use"
    echo "port ${KEYS_PORT}."
    exit 0
fi

# ── Post phase ─────────────────────────────────────────────────────────────────

# Load what the pre phase wrote (host, ports, prefix, name).
# shellcheck source=/dev/null
set -a; source "${PROJECT_DIR}/.env"; set +a

API="https://127.0.0.1:${SB_API_PORT}/${SB_API_PREFIX}"

echo "outline: waiting for ${CONTAINER} to start…"
MAX_WAIT=180
WAITED=0
until [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)" == "true" ]]; do
    if [[ $WAITED -ge $MAX_WAIT ]]; then
        echo "outline: container not running after ${MAX_WAIT}s. Check: docker logs ${CONTAINER}" >&2
        exit 1
    fi
    sleep 5
    WAITED=$(( WAITED + 5 ))
done
echo "outline: container running after ${WAITED}s"

# Wait for the management API to answer (self-signed cert → --insecure).
echo "outline: waiting for the management API…"
WAITED=0
until curl --insecure -sf "${API}/server" >/dev/null 2>&1; do
    if [[ $WAITED -ge $MAX_WAIT ]]; then
        echo "outline: management API not answering after ${MAX_WAIT}s. Check: docker logs ${CONTAINER}" >&2
        exit 1
    fi
    sleep 5
    WAITED=$(( WAITED + 5 ))
done
echo "outline: management API is up."

# Pin the port used for NEW access keys so there is a single predictable data port
# to open in the firewall (Outline otherwise picks a random port per key).
if curl --insecure -sf -X PUT -H "Content-Type: application/json" \
        -d "{\"port\":${KEYS_PORT}}" "${API}/server/port-for-new-access-keys" >/dev/null 2>&1; then
    echo "outline: new access keys will use port ${KEYS_PORT}."
else
    echo "outline: WARNING — could not set the access-key port to ${KEYS_PORT}." >&2
    echo "outline: it may be in use; set it manually in the Outline Manager." >&2
fi

# Set the human-readable server name shown in the Outline Manager.
curl --insecure -sf -X PUT -H "Content-Type: application/json" \
    -d "{\"name\":\"${SB_SERVER_NAME}\"}" "${API}/name" >/dev/null 2>&1 || true

# Shadowbox writes its config + keys as root into the bind mount. Chown back to the
# project owner so DELETE's rmtree can remove them (it ignores errors and would
# otherwise silently leave root-owned state behind).
OWNER="$(stat -c '%u:%g' "$PROJECT_DIR")"
docker exec "$CONTAINER" chown -R "$OWNER" /opt/outline/persisted-state 2>/dev/null || true

# Health checks — verify and warn only, never touch host networking ourselves.
if ss -Hltn 2>/dev/null | grep -qE "[:.]${SB_API_PORT}([[:space:]]|$)"; then
    echo "outline: management API bound on TCP ${SB_API_PORT}."
else
    echo "outline: WARNING — nothing listening on TCP ${SB_API_PORT}." >&2
fi
if ss -Hltn 2>/dev/null | grep -qE "[:.]${KEYS_PORT}([[:space:]]|$)"; then
    echo "outline: access keys bound on TCP ${KEYS_PORT}."
else
    echo "outline: NOTE — nothing on TCP ${KEYS_PORT} yet; it binds once the first key exists." >&2
fi

CERT_SHA256=$(openssl x509 -in "$CERT_FILE" -noout -sha256 -fingerprint 2>/dev/null | sed 's/.*=//;s/://g' || echo "")
API_URL="https://${OUTLINE_HOST}:${SB_API_PORT}/${SB_API_PREFIX}"

# Summary file, written atomically so a failed run never leaves a half-truth.
INFO_FILE="${PROJECT_DIR}/outline-info"
cat > "${INFO_FILE}.tmp" << INFO
# Outline Server (Shadowbox) — generated by the outline plugin install.sh
# Keep this file private: apiUrl + certSha256 grant full admin over the server.

HOST=${OUTLINE_HOST}
API_PORT=${SB_API_PORT}
KEYS_PORT=${KEYS_PORT}
API_URL=${API_URL}
CERT_SHA256=${CERT_SHA256}

# Outline Manager import string (paste into "Add server"):
# {"apiUrl":"${API_URL}","certSha256":"${CERT_SHA256}"}
# (also at ${PROJECT_DIR}/outline-access.txt)
#
# FIREWALL: allow ${SB_API_PORT}/tcp AND ${KEYS_PORT}/tcp+udp in the cloud security group.
# Manage keys from the Outline Manager desktop app. VPN clients use the Outline
# client app and connect to the access-key port.
# Remote shell (no host access): fhcli exec ${PROJECT_NAME} --service shadowbox
#
# Deleting this project deletes the server + ALL access keys.
INFO
mv "${INFO_FILE}.tmp" "$INFO_FILE"
chmod 600 "$INFO_FILE"

echo ""
echo "━━━  Outline Server setup complete  ━━━"
echo "Management API: ${API_URL}"
echo "certSha256:     ${CERT_SHA256}"
echo "Access keys:    port ${KEYS_PORT} (tcp+udp)"
echo "Import string:  ${PROJECT_DIR}/outline-access.txt   Summary: ${INFO_FILE}"
echo ""
echo "FIREWALL: allow ${SB_API_PORT}/tcp AND ${KEYS_PORT}/tcp+udp in the cloud security group."
exit 0
