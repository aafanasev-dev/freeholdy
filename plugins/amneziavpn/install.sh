#!/bin/bash
# install.sh — AmneziaWG VPN plugin setup.
#
# Interactive compose plugin (plugin.json "interactive": true): the pre phase runs
# attached to a WebSocket session (pty) so it can prompt the user.
#
#   Phase "pre"  (interactive, before docker compose up):
#     Seeds ./awg-data from the pinned image (awguser/awgstart live INSIDE
#     /etc/amnezia in the image — a bind mount of an empty dir would shadow
#     them), asks for the UDP port / client DNS / public IP / first client
#     name, writes serverip.cfg from the answers, and generates the AmneziaWG
#     obfuscation params (Jc..H4) keep-if-present — rotating them under
#     existing clients would break every distributed config.
#
#   Phase "post" (background thread, after docker compose up):
#     Creates the first client via the container's awguser (first run needs no
#     stdin but hardcodes user "metaligh"; we delete it and add the requested
#     name through awguser's numbered menu on stdin), applies with awgstart,
#     saves the client config + QR under ${PROJECT_DIR}/clients/, chowns the
#     volume back to the project owner (the container writes keys as root and
#     world-readable; DELETE's rmtree must be able to remove them), and writes
#     ${PROJECT_DIR}/amneziavpn-info. Post output is NOT streamed to the
#     install WebSocket — it lands in the service journal; the artifacts are
#     the files, which the pre phase tells the user about.
#
#   Both phases are idempotent: with zero exposed services this plugin creates
#   no ComposeService rows, so reconnecting to the install WebSocket after a
#   successful install re-runs pre against the live stack (existing .env
#   values become the defaults, keys are kept, post skips client creation).
#
# This is a raw UDP endpoint — freeholdy creates no subdomain, no nginx vhost,
# no certificate. The operator must open the UDP port in the cloud firewall.
# Everything lives inside the project dir — the freeholdy service user cannot
# write /var/lib or /etc and never touches host sysctl/iptables (the container
# NATs inside its own network namespace; forwarding is enabled per-namespace
# via the compose sysctls entry). Host checks are verify-and-warn only.
#
# Environment variables provided by freeholdy:
#   PLUGIN_DIR    — this plugin's source directory
#   PROJECT_DIR   — compose project directory (contains .env, compose files)
#   PROJECT_NAME  — project name (e.g. "vpn")
#   BASE_DOMAIN   — e.g. "your_domain.com"
#   (no SERVICE_*_LOCAL_PORT — nothing is exposed through nginx)

set -euo pipefail

PHASE="${1:-post}"

# Must match the digest pinned in docker-compose.yml — the pre phase seeds the
# volume from this exact image.
IMAGE="metaligh/amneziawg@sha256:b9c97c8c764e03e74e3fc270e4fee94dd260b81232944af50829b67935fecfe6"
AWG_CONTAINER="freeholdy_${PROJECT_NAME}_awg"
AWG_DIR="${PROJECT_DIR}/awg-data/amneziawg"   # host side of /etc/amnezia/amneziawg
AWG_IN="/etc/amnezia/amneziawg"               # container side
CLIENTS_DIR="${PROJECT_DIR}/clients"

# Idempotent KEY=VALUE write to the project .env (drop any previous line first).
# Values are single-quoted so the post phase can safely `source` the file even
# when a user-entered value contains shell metacharacters.
_env_set() {
    local key="$1" value="$2"
    grep -v "^${key}=" "${PROJECT_DIR}/.env" > "${PROJECT_DIR}/.env.tmp" || true
    mv "${PROJECT_DIR}/.env.tmp" "${PROJECT_DIR}/.env"
    printf "%s='%s'\n" "$key" "${value//\'/\'\\\'\'}" >> "${PROJECT_DIR}/.env"
}

# Keep-if-present variant: never rotate a value a previous run already wrote
# (changed obfuscation params break every distributed client config).
_env_keep() {
    local key="$1"
    grep -q "^${key}=" "${PROJECT_DIR}/.env" 2>/dev/null || _env_set "$key" "$2"
}

# Random int in [MIN, MAX] via $RANDOM arithmetic (no urandom pipes — under
# pipefail a `tr | head` chain dies with SIGPIPE and `set -e` kills the install).
_rand_range() {
    local min="$1" max="$2"
    echo $(( min + RANDOM % (max - min + 1) ))
}

# Random int in [5, 2147483647] (AmneziaWG header field range) — od reads
# /dev/urandom directly, no pipe-to-head.
_rand_u32() {
    local n
    n=$(od -An -N4 -tu4 /dev/urandom)
    echo $(( n % 2147483643 + 5 ))
}

# Is UDP $1 free to bind? Our own container holding it (re-run) counts as free.
_udp_port_free() {
    local port="$1"
    ss -lun 2>/dev/null | grep -qE "[:.]${port}([[:space:]]|$)" || return 0
    docker port "$AWG_CONTAINER" 2>/dev/null | grep -q ":${port}\$" && return 0
    return 1
}

_next_free_udp_port() {
    local p="$1"
    while ! _udp_port_free "$p"; do p=$(( p + 1 )); done
    echo "$p"
}

# ── Pre phase ──────────────────────────────────────────────────────────────────
if [[ "$PHASE" == "pre" ]]; then
    echo "AmneziaWG VPN setup — DPI-resistant WireGuard on a raw UDP port."
    echo "No subdomain, no SSL — nginx is not involved at all."
    echo ""

    RUNNING=$(docker inspect -f '{{.State.Running}}' "$AWG_CONTAINER" 2>/dev/null || echo false)
    if [[ "$RUNNING" == "true" ]]; then
        echo "NOTE: ${AWG_CONTAINER} is already running — this re-run keeps the"
        echo "server keys and obfuscation params; existing settings are the defaults."
        echo ""
    fi

    # Load prior answers (if any) as prompt defaults.
    # shellcheck source=/dev/null
    set -a; source "${PROJECT_DIR}/.env" 2>/dev/null || true; set +a
    PRIOR_PORT="${AWG_PORT:-}"

    mkdir -p "$CLIENTS_DIR" "${PROJECT_DIR}/awg-data"
    chmod 700 "$CLIENTS_DIR" "${PROJECT_DIR}/awg-data"

    # Seed the volume from the image: awguser/awgstart and the cfg templates
    # live under /etc/amnezia in the image, and an empty bind mount would
    # shadow them. docker cp writes the files as this (service) user, which is
    # also what lets DELETE's rmtree remove them later.
    if [[ ! -f "${AWG_DIR}/awguser" ]]; then
        echo "Seeding ./awg-data from the AmneziaWG image (~35 MB pull on first use)…"
        CID=$(docker create "$IMAGE")
        docker cp "${CID}:/etc/amnezia/." "${PROJECT_DIR}/awg-data/"
        docker rm "$CID" > /dev/null
        echo "Seeded."
    fi

    # UDP port. Host port == WireGuard listen port (written to serverip.cfg).
    DEF_PORT="${PRIOR_PORT:-51820}"
    _udp_port_free "$DEF_PORT" || DEF_PORT=$(_next_free_udp_port "$DEF_PORT")
    while :; do
        read -r -p "VPN UDP port [${DEF_PORT}]: " PORT_IN
        AWG_PORT="${PORT_IN:-$DEF_PORT}"
        if ! [[ "$AWG_PORT" =~ ^[0-9]+$ ]] || (( AWG_PORT < 1024 || AWG_PORT > 65535 )); then
            echo "'${AWG_PORT}' is not a valid port — use a number between 1024 and 65535."
            continue
        fi
        if ! _udp_port_free "$AWG_PORT"; then
            echo "UDP ${AWG_PORT} is already in use on this host — $(_next_free_udp_port "$AWG_PORT") is free."
            continue
        fi
        break
    done

    # DNS servers pushed to clients.
    DEF_DNS="${AWG_DNS:-1.1.1.1, 1.0.0.1}"
    while :; do
        read -r -p "DNS servers for VPN clients [${DEF_DNS}]: " DNS_IN
        AWG_DNS="${DNS_IN:-$DEF_DNS}"
        if [[ "$AWG_DNS" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+([[:space:]]*,[[:space:]]*[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)*$ ]]; then
            break
        fi
        echo "'${AWG_DNS}' doesn't look like comma-separated IPv4 addresses (e.g. 1.1.1.1, 1.0.0.1)."
    done

    # Public IP clients connect to. Try DNS on the base domain first (it should
    # already point at this VPS), then an external echo service.
    IP="${AWG_PUBLIC_IP:-}"
    [[ -z "$IP" ]] && IP=$(dig +short A "$BASE_DOMAIN" 2>/dev/null | head -n1 || true)
    [[ -z "$IP" ]] && IP=$(getent ahostsv4 "$BASE_DOMAIN" 2>/dev/null | awk 'NR==1{print $1}' || true)
    [[ -z "$IP" ]] && IP=$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || true)
    while :; do
        read -r -p "Public IP clients connect to [${IP:-required}]: " IP_IN
        IP="${IP_IN:-$IP}"
        [[ -n "$IP" ]] && break
        echo "A public IP is required — it goes into every client config as the endpoint."
    done

    # First client name (the post phase creates it once the container is up).
    DEF_CLIENT="${AWG_CLIENT1:-client1}"
    while :; do
        read -r -p "First client name [${DEF_CLIENT}]: " CLIENT_IN
        AWG_CLIENT1="${CLIENT_IN:-$DEF_CLIENT}"
        if [[ "$AWG_CLIENT1" == "awg0" ]]; then
            echo "'awg0' is the server's own config name — pick another."
            continue
        fi
        if [[ "$AWG_CLIENT1" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]]; then
            break
        fi
        echo "'${AWG_CLIENT1}' is not a valid name — use letters, digits, and _ - (must start alphanumeric)."
    done

    _env_set AWG_PORT "$AWG_PORT"
    _env_set AWG_DNS "$AWG_DNS"
    _env_set AWG_PUBLIC_IP "$IP"
    _env_set AWG_CLIENT1 "$AWG_CLIENT1"
    _env_set PROJECT_NAME "$PROJECT_NAME"   # for add-client.sh, which runs without freeholdy env

    # AmneziaWG obfuscation params — randomized per install so this server
    # doesn't share a fingerprint with every other deployment of the image.
    # Keep-if-present: stored in .env, vpn_params.cfg is rendered from there.
    S1=$(_rand_range 15 150)
    S2=$(_rand_range 15 150)
    while (( S1 + 56 == S2 )); do S2=$(_rand_range 15 150); done   # AWG constraint: S1+56 != S2
    H1=$(_rand_u32); H2=$(_rand_u32); H3=$(_rand_u32); H4=$(_rand_u32)
    while (( H2 == H1 )); do H2=$(_rand_u32); done
    while (( H3 == H1 || H3 == H2 )); do H3=$(_rand_u32); done
    while (( H4 == H1 || H4 == H2 || H4 == H3 )); do H4=$(_rand_u32); done
    _env_keep AWG_JC "$(_rand_range 3 10)"
    _env_keep AWG_JMIN "$(_rand_range 40 80)"
    _env_keep AWG_JMAX "$(_rand_range 800 1200)"
    _env_keep AWG_S1 "$S1"
    _env_keep AWG_S2 "$S2"
    _env_keep AWG_H1 "$H1"
    _env_keep AWG_H2 "$H2"
    _env_keep AWG_H3 "$H3"
    _env_keep AWG_H4 "$H4"

    # Re-load: the keep-if-present values may be older than this run's candidates.
    # shellcheck source=/dev/null
    set -a; source "${PROJECT_DIR}/.env"; set +a

    # serverip.cfg is line-positional: 1) public IP  2) client DNS  3) UDP port.
    printf '%s\n' "$AWG_PUBLIC_IP" "$AWG_DNS" "$AWG_PORT" > "${AWG_DIR}/serverip.cfg"

    cat > "${AWG_DIR}/vpn_params.cfg" << PARAMS
Jc = ${AWG_JC}
Jmin = ${AWG_JMIN}
Jmax = ${AWG_JMAX}
S1 = ${AWG_S1}
S2 = ${AWG_S2}
H1 = ${AWG_H1}
H2 = ${AWG_H2}
H3 = ${AWG_H3}
H4 = ${AWG_H4}
PARAMS

    echo ""
    echo "Configured: endpoint ${AWG_PUBLIC_IP}:${AWG_PORT}/udp, first client '${AWG_CLIENT1}'."
    if [[ "$RUNNING" == "true" && -n "$PRIOR_PORT" && "$PRIOR_PORT" != "$AWG_PORT" ]]; then
        echo ""
        echo "WARNING: the port changed (${PRIOR_PORT} → ${AWG_PORT}). Existing client"
        echo "configs still point at ${PRIOR_PORT} — re-create them (./add-client.sh)"
        echo "and update your firewall rule."
    fi
    echo ""
    echo "After the install finishes (about a minute), your client config appears at:"
    echo "  ${CLIENTS_DIR}/${AWG_CLIENT1}.conf      (import into an AmneziaVPN app)"
    echo "  ${CLIENTS_DIR}/${AWG_CLIENT1}.qr.txt    (QR code — 'cat' it in a terminal)"
    echo "View or add clients later: ${PROJECT_DIR}/add-client.sh — see ABOUT for details."
    echo ""
    echo "IMPORTANT: stock WireGuard clients can NOT connect — use the AmneziaVPN"
    echo "apps or amneziawg tooling."
    echo "IMPORTANT: UDP port ${AWG_PORT} must be reachable from the internet (cloud"
    echo "provider security group / firewall) or clients will never connect."
    exit 0
fi

# ── Post phase ─────────────────────────────────────────────────────────────────

# Load what the pre phase wrote (port, client name, endpoint IP).
# shellcheck source=/dev/null
set -a; source "${PROJECT_DIR}/.env"; set +a

echo "amneziavpn: waiting for ${AWG_CONTAINER} to start…"
MAX_WAIT=180
WAITED=0
until [[ "$(docker inspect -f '{{.State.Running}}' "$AWG_CONTAINER" 2>/dev/null || echo false)" == "true" ]]; do
    if [[ $WAITED -ge $MAX_WAIT ]]; then
        echo "amneziavpn: container not running after ${MAX_WAIT}s. Check: docker logs ${AWG_CONTAINER}" >&2
        exit 1
    fi
    sleep 5
    WAITED=$(( WAITED + 5 ))
done
echo "amneziavpn: container running after ${WAITED}s"

# Create the first client. awguser's first run (no *.conf yet) needs no stdin
# but hardcodes a user named "metaligh"; afterwards it is a numbered menu on
# stdin (2 = add user, 3 = delete user). Filter the ANSI QR dump out of the log.
# add-client.sh repeats this exec sequence — keep the two in sync.
if docker exec "$AWG_CONTAINER" test -f "${AWG_IN}/${AWG_CLIENT1}.conf" 2>/dev/null; then
    echo "amneziavpn: client '${AWG_CLIENT1}' already exists — keeping it."
elif docker exec "$AWG_CONTAINER" test -f "${AWG_IN}/awg0.conf" 2>/dev/null; then
    echo "amneziavpn: adding client '${AWG_CLIENT1}'…"
    OUT=$(printf '2\n%s\n' "$AWG_CLIENT1" | docker exec -i "$AWG_CONTAINER" awguser 2>&1)
    grep -v "37;1m" <<< "$OUT" || true
else
    echo "amneziavpn: initializing server keys and first client…"
    OUT=$(docker exec "$AWG_CONTAINER" awguser 2>&1)
    grep -v "37;1m" <<< "$OUT" || true
    if [[ "$AWG_CLIENT1" != "metaligh" ]]; then
        printf '3\nmetaligh\n' | docker exec -i "$AWG_CONTAINER" awguser > /dev/null
        OUT=$(printf '2\n%s\n' "$AWG_CLIENT1" | docker exec -i "$AWG_CONTAINER" awguser 2>&1)
        grep -v "37;1m" <<< "$OUT" || true
    fi
fi

# Apply the config (awgstart = awg-quick down + up; safe to re-run, brief blip).
STARTED=no
for _ in 1 2 3; do
    if docker exec "$AWG_CONTAINER" awgstart > /dev/null 2>&1; then
        STARTED=yes
        break
    fi
    sleep 3
done
if [[ "$STARTED" != "yes" ]]; then
    echo "amneziavpn: awgstart failed. Check: docker logs ${AWG_CONTAINER}" >&2
    exit 1
fi
echo "amneziavpn: AmneziaWG is up on UDP ${AWG_PORT}."

# Save the client config + QR. Read via docker exec — the container writes the
# volume as root and the service user may not be able to read it from the host.
mkdir -p "$CLIENTS_DIR"
chmod 700 "$CLIENTS_DIR"
docker exec "$AWG_CONTAINER" cat "${AWG_IN}/${AWG_CLIENT1}.conf" > "${CLIENTS_DIR}/${AWG_CLIENT1}.conf"
chmod 600 "${CLIENTS_DIR}/${AWG_CLIENT1}.conf"

# Build the Amnezia native vpn:// code (apps import that, not raw WireGuard
# .conf). Derive the client public key inside the container so the host needs
# no crypto libs. QR encodes the vpn:// code; fall back to the .conf if either
# the conversion or qrencode is unavailable.
CLIENT_PRIV="$(grep -oP '(?<=^PrivateKey = ).*' "${CLIENTS_DIR}/${AWG_CLIENT1}.conf" | head -1)"
CLIENT_PUB="$(printf '%s' "$CLIENT_PRIV" | docker exec -i "$AWG_CONTAINER" awg pubkey 2>/dev/null || true)"
QR_SRC="${CLIENTS_DIR}/${AWG_CLIENT1}.conf"
if python3 "${PROJECT_DIR}/conf2vpn.py" "${CLIENTS_DIR}/${AWG_CLIENT1}.conf" \
        --name "${PROJECT_NAME}" ${CLIENT_PUB:+--pubkey "$CLIENT_PUB"} \
        > "${CLIENTS_DIR}/${AWG_CLIENT1}.vpn.txt" 2>/dev/null; then
    chmod 600 "${CLIENTS_DIR}/${AWG_CLIENT1}.vpn.txt"
    QR_SRC="${CLIENTS_DIR}/${AWG_CLIENT1}.vpn.txt"
else
    rm -f "${CLIENTS_DIR}/${AWG_CLIENT1}.vpn.txt"
    echo "amneziavpn: WARNING — could not build the vpn:// code; QR falls back to raw .conf." >&2
fi
if docker exec -i "$AWG_CONTAINER" qrencode -t UTF8 < "$QR_SRC" > "${CLIENTS_DIR}/${AWG_CLIENT1}.qr.txt" 2>/dev/null; then
    chmod 600 "${CLIENTS_DIR}/${AWG_CLIENT1}.qr.txt"
    echo "amneziavpn: client config + vpn:// code + QR saved under ${CLIENTS_DIR}/"
else
    rm -f "${CLIENTS_DIR}/${AWG_CLIENT1}.qr.txt"
    echo "amneziavpn: client config saved (QR rendering unavailable): ${CLIENTS_DIR}/${AWG_CLIENT1}.conf"
fi

# The container writes keys as root and world-readable. Chown to the project
# owner so DELETE's rmtree (ignore_errors=True — it would skip root-owned key
# material silently) can actually remove them, and close the permissions.
OWNER="$(stat -c '%u:%g' "$PROJECT_DIR")"
docker exec "$AWG_CONTAINER" chown -R "$OWNER" /etc/amnezia
chmod -R go-rwx "${PROJECT_DIR}/awg-data"

# Health checks — verify and warn only, never touch host networking ourselves.
if ss -lun | grep -qE "[:.]${AWG_PORT}([[:space:]]|$)"; then
    echo "amneziavpn: UDP ${AWG_PORT} is bound on the host."
else
    echo "amneziavpn: WARNING — nothing is listening on UDP ${AWG_PORT}." >&2
    echo "amneziavpn: Check: docker logs ${AWG_CONTAINER}" >&2
fi
if [[ "$(sysctl -n net.ipv4.ip_forward 2>/dev/null || echo 0)" != "1" ]]; then
    echo "amneziavpn: WARNING — net.ipv4.ip_forward=0 on the host. Docker normally" >&2
    echo "amneziavpn: enables it; without it VPN clients cannot reach the internet." >&2
    echo "amneziavpn: Operator fix (as root): sysctl -w net.ipv4.ip_forward=1" >&2
fi

# Summary file, written atomically so a failed run never leaves a half-truth.
INFO_FILE="${PROJECT_DIR}/amneziavpn-info"
cat > "${INFO_FILE}.tmp" << INFO
# AmneziaWG VPN — generated by the amneziavpn plugin install.sh
# Keep this file private: client configs grant full VPN access.

ENDPOINT=${AWG_PUBLIC_IP}:${AWG_PORT}/udp
FIRST_CLIENT=${AWG_CLIENT1}
CLIENT_CONFIG=${CLIENTS_DIR}/${AWG_CLIENT1}.conf
CLIENT_VPN=${CLIENTS_DIR}/${AWG_CLIENT1}.vpn.txt
CLIENT_QR=${CLIENTS_DIR}/${AWG_CLIENT1}.qr.txt

# Clients: use AmneziaVPN apps or amneziawg tooling — stock WireGuard will NOT
# connect (the obfuscation params are deliberate protocol changes).
#
# Manage clients:   ${PROJECT_DIR}/add-client.sh NAME | --show NAME | --remove NAME
# Remote (no shell on the host): fhcli exec ${PROJECT_NAME} --service awg
#   then run: awguser   (menu)  and  awgstart  (apply)
#
# FIREWALL: UDP ${AWG_PORT} must be allowed in by the cloud security group.
# Deleting this project deletes the server keys — every client config dies with it.
INFO
mv "${INFO_FILE}.tmp" "$INFO_FILE"
chmod 600 "$INFO_FILE"

echo ""
echo "━━━  AmneziaWG VPN setup complete  ━━━"
echo "Endpoint: ${AWG_PUBLIC_IP}:${AWG_PORT}/udp   first client: ${AWG_CLIENT1}"
echo "Client config: ${CLIENTS_DIR}/${AWG_CLIENT1}.conf"
echo "Connect (AmneziaVPN app): scan QR  'cat ${CLIENTS_DIR}/${AWG_CLIENT1}.qr.txt'"
echo "                          or paste 'cat ${CLIENTS_DIR}/${AWG_CLIENT1}.vpn.txt' (vpn:// code)"
echo "Summary: ${INFO_FILE}"
echo ""
echo "FIREWALL: your cloud provider's security group MUST allow UDP ${AWG_PORT} in."
echo "Docker-published ports bypass ufw on stock installs, but if you filter the"
echo "DOCKER-USER chain add: ufw allow ${AWG_PORT}/udp"
exit 0
