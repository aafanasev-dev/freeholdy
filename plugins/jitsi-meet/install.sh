#!/bin/bash
# install.sh — Jitsi Meet plugin setup.
#
# Interactive compose plugin (plugin.json "interactive": true): the pre phase runs
# attached to a WebSocket session (pty) so it can prompt the user.
#
#   Phase "pre"  (interactive, before docker compose up):
#     Creates the ./jitsi-cfg volume tree, asks for the moderator account
#     (secure-domain auth: only logged-in users start meetings, guests join),
#     detects the server's public IP for JVB_ADVERTISE_IPS (wrong IP = remote
#     participants get no audio/video), and writes everything to
#     ${PROJECT_DIR}/.env. The internal XMPP secrets (JICOFO_AUTH_PASSWORD,
#     JVB_AUTH_PASSWORD) are generated keep-if-present — a pre re-run after an
#     early failure must not desync them from any half-generated jitsi-cfg state.
#
#   Phase "post" (background thread, after docker compose up):
#     Waits for prosody to generate its config, registers the moderator account
#     via prosodyctl, health-checks beyond "the page loads" (web answers on its
#     loopback port, UDP 10000 is bound on the host, all 4 containers running),
#     saves credentials to ${PROJECT_DIR}/jitsi-credentials (mode 600), and
#     prints the firewall + 3-participant-test reminders.
#
# Everything lives inside the project dir — the freeholdy service user cannot
# write /var/lib or /etc (and cannot run ufw: opening UDP 10000 in the host or
# cloud firewall is the operator's manual step). DELETE /projects/{name} tears
# it all down together.
#
# Environment variables provided by freeholdy:
#   PLUGIN_DIR    — this plugin's source directory
#   PROJECT_DIR   — compose project directory (contains .env, compose files)
#   PROJECT_NAME  — project name (e.g. "meet")
#   BASE_DOMAIN   — e.g. "your_domain.com"
#   SERVICE_WEB_LOCAL_PORT — (post phase only) web's allocated loopback port

set -euo pipefail

PHASE="${1:-post}"

# 32 hex chars. (openssl, not a `tr </dev/urandom | head` pipe — under pipefail
# that dies with SIGPIPE and `set -e` silently kills the whole install.)
_gen_pass() {
    openssl rand -hex 16
}

# Idempotent KEY=VALUE write to the project .env (drop any previous line first).
# Values are single-quoted so the post phase can safely `source` the file even
# when a user-entered password contains shell metacharacters.
_env_set() {
    local key="$1" value="$2"
    grep -v "^${key}=" "${PROJECT_DIR}/.env" > "${PROJECT_DIR}/.env.tmp" || true
    mv "${PROJECT_DIR}/.env.tmp" "${PROJECT_DIR}/.env"
    printf "%s='%s'\n" "$key" "${value//\'/\'\\\'\'}" >> "${PROJECT_DIR}/.env"
}

# Keep-if-present variant: never rotate a secret a previous run already wrote
# (mismatched internal XMPP passwords break jicofo/prosody startup).
_env_keep() {
    local key="$1"
    grep -q "^${key}=" "${PROJECT_DIR}/.env" 2>/dev/null || _env_set "$key" "$2"
}

# ── Pre phase ──────────────────────────────────────────────────────────────────
if [[ "$PHASE" == "pre" ]]; then
    echo "Jitsi Meet setup — video conferencing at https://meet.${BASE_DOMAIN}"
    echo ""

    mkdir -p "${PROJECT_DIR}"/jitsi-cfg/{web,prosody/config,prosody/prosody-plugins-custom,jicofo,jvb}

    # Moderator account (secure domain: meetings start only after a moderator
    # logs in; guests join without an account).
    while :; do
        read -r -p "Moderator username [moderator]: " MOD_USER
        MOD_USER="${MOD_USER:-moderator}"
        if [[ "$MOD_USER" =~ ^[a-z0-9][a-z0-9._-]*$ ]]; then
            break
        fi
        echo "'${MOD_USER}' is not a valid username — use lowercase letters, digits, and . _ -"
    done
    read -r -p "Moderator password [blank = auto-generate]: " MOD_PASS
    MOD_PASS="${MOD_PASS:-$(_gen_pass)}"

    # Public IP for WebRTC media (JVB_ADVERTISE_IPS). Try DNS on the base domain
    # first (it should already point at this VPS), then an external echo service.
    IP=$(dig +short A "$BASE_DOMAIN" 2>/dev/null | head -n1 || true)
    [[ -z "$IP" ]] && IP=$(getent ahostsv4 "$BASE_DOMAIN" 2>/dev/null | awk 'NR==1{print $1}' || true)
    [[ -z "$IP" ]] && IP=$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || true)
    while :; do
        read -r -p "Public IP for WebRTC media (JVB_ADVERTISE_IPS) [${IP:-required}]: " IP_IN
        IP="${IP_IN:-$IP}"
        [[ -n "$IP" ]] && break
        echo "A public IP is required — without it remote participants get no audio/video."
    done

    _env_set BASE_DOMAIN "$BASE_DOMAIN"
    _env_set JVB_ADVERTISE_IPS "$IP"
    _env_set JITSI_MODERATOR_USER "$MOD_USER"
    _env_set JITSI_MODERATOR_PASS "$MOD_PASS"
    _env_keep JICOFO_AUTH_PASSWORD "$(_gen_pass)"
    _env_keep JVB_AUTH_PASSWORD "$(_gen_pass)"

    echo ""
    echo "Configured: https://meet.${BASE_DOMAIN}, moderator '${MOD_USER}', media IP ${IP}."
    echo "IMPORTANT: UDP port 10000 must be reachable from the internet (cloud"
    echo "provider security group / firewall) or calls with 3+ participants will"
    echo "have no audio/video."
    exit 0
fi

# ── Post phase ─────────────────────────────────────────────────────────────────

# Load what the pre phase wrote (JVB_ADVERTISE_IPS, moderator account, secrets).
# shellcheck source=/dev/null
set -a; source "${PROJECT_DIR}/.env"; set +a

PROSODY="freeholdy_${PROJECT_NAME}_prosody"
CREDS_FILE="${PROJECT_DIR}/jitsi-credentials"

# Wait for prosody's entrypoint to generate its config on first boot.
echo "jitsi-meet: waiting for ${PROSODY} to generate its config…"
MAX_WAIT=300
WAITED=0
until docker exec "$PROSODY" test -f /config/prosody.cfg.lua 2>/dev/null; do
    if [[ $WAITED -ge $MAX_WAIT ]]; then
        echo "jitsi-meet: prosody not ready after ${MAX_WAIT}s. Check: docker logs ${PROSODY}" >&2
        exit 1
    fi
    sleep 5
    WAITED=$(( WAITED + 5 ))
done
echo "jitsi-meet: prosody config present after ${WAITED}s"

# Register the moderator (idempotent: an already-registered account is fine).
REG=failed
for _ in $(seq 1 12); do
    if OUT=$(docker exec "$PROSODY" prosodyctl --config /config/prosody.cfg.lua \
             register "$JITSI_MODERATOR_USER" meet.jitsi "$JITSI_MODERATOR_PASS" 2>&1); then
        REG=ok
        break
    fi
    if echo "$OUT" | grep -qi "exists"; then
        REG=exists
        break
    fi
    sleep 5
done
case "$REG" in
    ok)     echo "jitsi-meet: moderator '${JITSI_MODERATOR_USER}' registered." ;;
    exists) echo "jitsi-meet: moderator '${JITSI_MODERATOR_USER}' already registered — keeping it." ;;
    *)      echo "jitsi-meet: could not register the moderator. Run manually:" >&2
            echo "  docker exec ${PROSODY} prosodyctl --config /config/prosody.cfg.lua register ${JITSI_MODERATOR_USER} meet.jitsi '<password>'" >&2 ;;
esac

# Health checks beyond "the page loads".
WEB_OK=no
for _ in $(seq 1 24); do
    if curl -fsS -o /dev/null "http://127.0.0.1:${SERVICE_WEB_LOCAL_PORT}/" 2>/dev/null; then
        WEB_OK=yes
        break
    fi
    sleep 5
done
[[ "$WEB_OK" == "yes" ]] \
    && echo "jitsi-meet: web answering on 127.0.0.1:${SERVICE_WEB_LOCAL_PORT}" \
    || echo "jitsi-meet: WARNING — web not answering on 127.0.0.1:${SERVICE_WEB_LOCAL_PORT}. Check: docker logs freeholdy_${PROJECT_NAME}_web" >&2

if ss -lun | grep -q ':10000 '; then
    echo "jitsi-meet: UDP 10000 is bound on the host (media port published)."
else
    echo "jitsi-meet: WARNING — nothing is listening on UDP 10000. WebRTC media is" >&2
    echo "broken for 3+ participant calls. Check: docker logs freeholdy_${PROJECT_NAME}_jvb" >&2
fi

for SVC in web prosody jicofo jvb; do
    if [[ "$(docker inspect -f '{{.State.Running}}' "freeholdy_${PROJECT_NAME}_${SVC}" 2>/dev/null || echo false)" != "true" ]]; then
        echo "jitsi-meet: WARNING — container freeholdy_${PROJECT_NAME}_${SVC} is not running." >&2
    fi
done

cat > "$CREDS_FILE" << CREDS
# Jitsi Meet credentials — generated by the jitsi-meet plugin install.sh
# Keep this file secret: chmod 600 $CREDS_FILE

URL=https://meet.${BASE_DOMAIN}
MODERATOR_USER=${JITSI_MODERATOR_USER}
MODERATOR_PASS=${JITSI_MODERATOR_PASS}

# Only the moderator can start a meeting ("I am the host" → log in).
# Guests need no account but wait until a moderator has joined the room.
CREDS
chmod 600 "$CREDS_FILE"
echo "jitsi-meet: credentials saved to ${CREDS_FILE}"

echo ""
echo "━━━  Jitsi Meet plugin setup complete  ━━━"
echo "URL: https://meet.${BASE_DOMAIN}   (moderator: ${JITSI_MODERATOR_USER})"
echo ""
echo "FIREWALL: your cloud provider's security group MUST allow UDP 10000 in."
echo "Docker-published ports bypass ufw on stock installs, but if you filter the"
echo "DOCKER-USER chain add: ufw allow 10000/udp"
echo ""
echo "TEST WITH 3+ PARTICIPANTS: 2-person calls use peer-to-peer and never touch"
echo "the videobridge, so they can work while UDP 10000 / JVB_ADVERTISE_IPS"
echo "(currently ${JVB_ADVERTISE_IPS}) are broken. Media for the 3rd participant"
echo "is the real test."
exit 0
