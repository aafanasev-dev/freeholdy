#!/bin/bash
# add-client.sh — manage AmneziaWG VPN clients after install.
#
# Staged into the project dir by the amneziavpn plugin. Runs on the host as
# whoever invokes it — needs docker access and write access to ./clients
# (in practice: the freeholdy service user or root).
#
#   ./add-client.sh NAME           add a client, save conf + QR, print both
#   ./add-client.sh --show NAME    print a saved client config + QR
#   ./add-client.sh --remove NAME  remove a client (their config stops working)
#
# Adding or removing a client re-applies the server config (awgstart =
# awg-quick down + up), which briefly drops live tunnels.
#
# The docker exec sequences mirror install.sh's post phase — keep them in sync.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# PROJECT_NAME is written to .env by install.sh's pre phase; fall back to the
# directory name (project dirs are PROJECTS_DIR/{name}).
# shellcheck source=/dev/null
set -a; source "${PROJECT_DIR}/.env" 2>/dev/null || true; set +a
PROJECT_NAME="${PROJECT_NAME:-$(basename "$PROJECT_DIR")}"

AWG_CONTAINER="freeholdy_${PROJECT_NAME}_awg"
AWG_IN="/etc/amnezia/amneziawg"
CLIENTS_DIR="${PROJECT_DIR}/clients"

usage() {
    echo "Usage: $0 NAME | --show NAME | --remove NAME" >&2
    exit 1
}

ACTION="add"
case "${1:-}" in
    --show)   ACTION="show";   NAME="${2:-}" ;;
    --remove) ACTION="remove"; NAME="${2:-}" ;;
    --*)      usage ;;
    *)        NAME="${1:-}" ;;
esac
[[ -z "${NAME}" ]] && usage

if [[ "$NAME" == "awg0" ]] || ! [[ "$NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]]; then
    echo "'${NAME}' is not a valid client name — use letters, digits, and _ - (and not 'awg0')." >&2
    exit 1
fi

_require_running() {
    if [[ "$(docker inspect -f '{{.State.Running}}' "$AWG_CONTAINER" 2>/dev/null || echo false)" != "true" ]]; then
        echo "Container ${AWG_CONTAINER} is not running — start the project first." >&2
        exit 1
    fi
}

# The container writes the volume as root; give it back to the project owner so
# DELETE's rmtree can remove the keys, and keep permissions closed.
_fix_volume_perms() {
    docker exec "$AWG_CONTAINER" chown -R "$(stat -c '%u:%g' "$PROJECT_DIR")" /etc/amnezia
    chmod -R go-rwx "${PROJECT_DIR}/awg-data" 2>/dev/null || true
}

# Save NAME's config from the container, build the Amnezia native `vpn://` code,
# and render its QR; print all three. The AmneziaVPN apps import the vpn:// blob
# (not raw WireGuard .conf), so the QR and text code both encode that.
_save_and_show() {
    mkdir -p "$CLIENTS_DIR"
    chmod 700 "$CLIENTS_DIR" 2>/dev/null || true
    docker exec "$AWG_CONTAINER" cat "${AWG_IN}/${NAME}.conf" > "${CLIENTS_DIR}/${NAME}.conf"
    chmod 600 "${CLIENTS_DIR}/${NAME}.conf"

    # Derive the client public key inside the container so the host needs no
    # crypto libs, then convert the .conf into the Amnezia vpn:// string.
    local priv pub
    priv="$(grep -oP '(?<=^PrivateKey = ).*' "${CLIENTS_DIR}/${NAME}.conf" | head -1)"
    pub="$(printf '%s' "$priv" | docker exec -i "$AWG_CONTAINER" awg pubkey 2>/dev/null || true)"
    if python3 "${PROJECT_DIR}/conf2vpn.py" "${CLIENTS_DIR}/${NAME}.conf" \
            --name "${NAME}" ${pub:+--pubkey "$pub"} \
            > "${CLIENTS_DIR}/${NAME}.vpn.txt" 2>/dev/null; then
        chmod 600 "${CLIENTS_DIR}/${NAME}.vpn.txt"
    else
        rm -f "${CLIENTS_DIR}/${NAME}.vpn.txt"
    fi

    # QR encodes the vpn:// code (falls back to the raw .conf if conversion failed).
    local qr_src="${CLIENTS_DIR}/${NAME}.vpn.txt"
    [[ -f "$qr_src" ]] || qr_src="${CLIENTS_DIR}/${NAME}.conf"
    if docker exec -i "$AWG_CONTAINER" qrencode -t UTF8 < "$qr_src" > "${CLIENTS_DIR}/${NAME}.qr.txt" 2>/dev/null; then
        chmod 600 "${CLIENTS_DIR}/${NAME}.qr.txt"
    else
        rm -f "${CLIENTS_DIR}/${NAME}.qr.txt"
    fi

    echo ""
    echo "── ${CLIENTS_DIR}/${NAME}.conf ──"
    cat "${CLIENTS_DIR}/${NAME}.conf"
    if [[ -f "${CLIENTS_DIR}/${NAME}.qr.txt" ]]; then
        echo ""
        cat "${CLIENTS_DIR}/${NAME}.qr.txt"
    fi
    if [[ -f "${CLIENTS_DIR}/${NAME}.vpn.txt" ]]; then
        echo ""
        echo "── vpn:// code (paste into the app's 'Add by code/text') ──"
        cat "${CLIENTS_DIR}/${NAME}.vpn.txt"
        echo ""
    fi
    echo ""
    echo "Import into an AmneziaVPN app (scan the QR or paste the vpn:// code) —"
    echo "stock WireGuard clients will NOT connect."
}

case "$ACTION" in
    add)
        _require_running
        if docker exec "$AWG_CONTAINER" test -f "${AWG_IN}/${NAME}.conf" 2>/dev/null; then
            echo "Client '${NAME}' already exists — use: $0 --show ${NAME}" >&2
            exit 1
        fi
        if ! docker exec "$AWG_CONTAINER" test -f "${AWG_IN}/awg0.conf" 2>/dev/null; then
            echo "Server is not initialized (no awg0.conf) — the plugin install did not finish." >&2
            exit 1
        fi
        echo "Adding client '${NAME}'…"
        printf '2\n%s\n' "$NAME" | docker exec -i "$AWG_CONTAINER" awguser > /dev/null
        docker exec "$AWG_CONTAINER" awgstart > /dev/null
        _fix_volume_perms
        echo "Client '${NAME}' added and the server config re-applied (live tunnels blip)."
        _save_and_show
        ;;
    show)
        if [[ ! -f "${CLIENTS_DIR}/${NAME}.conf" ]]; then
            _require_running
            if ! docker exec "$AWG_CONTAINER" test -f "${AWG_IN}/${NAME}.conf" 2>/dev/null; then
                echo "No such client '${NAME}'." >&2
                exit 1
            fi
        fi
        _require_running
        _save_and_show
        ;;
    remove)
        _require_running
        if ! docker exec "$AWG_CONTAINER" test -f "${AWG_IN}/${NAME}.conf" 2>/dev/null; then
            echo "No such client '${NAME}'." >&2
            exit 1
        fi
        printf '3\n%s\n' "$NAME" | docker exec -i "$AWG_CONTAINER" awguser > /dev/null
        docker exec "$AWG_CONTAINER" awgstart > /dev/null
        _fix_volume_perms
        rm -f "${CLIENTS_DIR}/${NAME}.conf" "${CLIENTS_DIR}/${NAME}.qr.txt" "${CLIENTS_DIR}/${NAME}.vpn.txt"
        echo "Client '${NAME}' removed — their config no longer connects."
        ;;
esac
