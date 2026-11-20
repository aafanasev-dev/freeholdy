#!/usr/bin/env bash
# =============================================================================
# setup_nginx_stream.sh — wire nginx's `stream` (raw TCP) module in for freeholdy.
#
# Compose services labelled `freeholdy.proxy: tcp` (e.g. the postgresql plugin) are
# proxied with nginx `stream` servers rather than HTTP vhosts. freeholdy writes those
# into NGINX_STREAMS_DIR (/etc/nginx/freeholdy-streams); this script makes nginx read
# that directory from its MAIN context:
#
#     stream { include /etc/nginx/freeholdy-streams/*.conf; }
#
# Idempotent, run as root. Called by install.sh and update.sh (non-fatal there), and
# safe to run by hand:  sudo bash scripts/setup_nginx_stream.sh
#
# What it does (never edits another app's config; reverts on any `nginx -t` failure):
#   1. requires nginx >= 1.21.4 (stream `ssl_alpn`, which PostgreSQL's direct TLS needs)
#   2. makes sure the stream module is available — built in, already loaded, or
#      `apt-get install libnginx-mod-stream` (only with -i or after a confirmation,
#      because on a shared box that is an apt change next to someone else's nginx)
#   3. creates /etc/nginx/freeholdy-streams{,/sni} (root:GROUP 1775, like the sites
#      dirs) and the socket dir /var/lib/freeholdy-nginx
#   4. adds the stream {} block: via /etc/nginx/modules-enabled/99-freeholdy-stream.conf
#      when nginx.conf includes that dir (Debian/Ubuntu), otherwise by appending one
#      include line to nginx.conf (backed up first)
#   5. nginx -t → reload
# If nginx already has a stream {} block of its own, nginx allows no second one: the
# script prints the single include line to add to it and stops (exit 2).
#
# The freeholdy-streams directory existing is how the server knows stream support is
# ready (nginx_service.stream_supported), so it is only created once nginx reads it.
#
# Options:
#   -g GROUP  group that may write the stream dirs (default: nginx-managers)
#   -i        install the stream module package without asking if it is missing
#   -y        assume "yes" to confirmations
#   -q        quieter output
# Exit codes: 0 ready · 1 error (nothing left changed) · 2 skipped / needs manual action
# =============================================================================

set -euo pipefail

GROUP="nginx-managers"
AUTO_INSTALL=0
ASSUME_YES=0
QUIET=0
while getopts ":g:iyq" opt; do
    case "$opt" in
        g) GROUP="$OPTARG" ;;
        i) AUTO_INSTALL=1 ;;
        y) ASSUME_YES=1 ;;
        q) QUIET=1 ;;
        *) echo "usage: $0 [-g GROUP] [-i] [-y] [-q]" >&2; exit 1 ;;
    esac
done

STREAMS_DIR="/etc/nginx/freeholdy-streams"
SOCKET_DIR="/var/lib/freeholdy-nginx"
MODULES_ENABLED="/etc/nginx/modules-enabled"
OUR_MODULE_CONF="${MODULES_ENABLED}/99-freeholdy-stream.conf"
OUR_MAIN_CONF="/etc/nginx/freeholdy-stream-main.conf"
NGINX_CONF="/etc/nginx/nginx.conf"
INCLUDE_LINE="include ${STREAMS_DIR}/*.conf;"
MIN_VERSION="1.21.4"

RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { [[ "$QUIET" -eq 1 ]] || echo -e "  ${GREEN}✓${NC}  $*"; }
warn() { echo -e "  ${YELLOW}⚠${NC}  $*"; }
info() { [[ "$QUIET" -eq 1 ]] || echo -e "  ${CYAN}→${NC}  $*"; }
fail() { echo -e "  ${RED}✗${NC}  $*" >&2; }

confirm() {
    [[ "$ASSUME_YES" -eq 1 ]] && return 0
    [[ -r /dev/tty ]] || return 1
    local reply
    read -r -p "  $1 [y/N] " reply < /dev/tty || return 1
    [[ "$reply" =~ ^[Yy]([Ee][Ss])?$ ]]
}

if [[ $EUID -ne 0 ]]; then
    fail "must run as root (sudo bash $0)"
    exit 1
fi

# ── 1. nginx present + recent enough ────────────────────────────────────────────
if ! command -v nginx &>/dev/null; then
    warn "nginx is not installed — skipping stream setup (TCP plugins like postgresql unavailable)"
    exit 2
fi
NGINX_VERSION="$(nginx -v 2>&1 | sed -n 's#.*nginx/\([0-9.]*\).*#\1#p')"
if [[ -z "$NGINX_VERSION" ]] \
   || [[ "$(printf '%s\n%s\n' "$MIN_VERSION" "$NGINX_VERSION" | sort -V | head -1)" != "$MIN_VERSION" ]]; then
    warn "nginx ${NGINX_VERSION:-?} is older than ${MIN_VERSION} (stream ssl_alpn) — skipping stream setup"
    exit 2
fi
ok "nginx ${NGINX_VERSION}"

if ! nginx -t &>/dev/null; then
    fail "nginx -t fails on the CURRENT config — refusing to touch a broken nginx:"
    nginx -t 2>&1 | sed 's/^/      /' >&2 || true
    exit 1
fi

DUMP="$(nginx -T 2>/dev/null || true)"
BUILD="$(nginx -V 2>&1 || true)"

# ── 2. Already wired? ──────────────────────────────────────────────────────────
# Our include line showing up in the effective config means a stream {} block (ours,
# or the operator's own with our line added by hand) already reads the directory.
already_wired() { grep -qF "${STREAMS_DIR}/*.conf" <<<"$DUMP"; }

ensure_dirs() {
    getent group "$GROUP" &>/dev/null || GROUP="root"
    mkdir -p "${STREAMS_DIR}/sni"
    for d in "$STREAMS_DIR" "${STREAMS_DIR}/sni"; do
        chgrp "$GROUP" "$d"
        chmod 1775 "$d"
    done
    mkdir -p "$SOCKET_DIR"
    chmod 0755 "$SOCKET_DIR"
    ok "stream dirs ready: ${STREAMS_DIR} (root:${GROUP} 1775), sockets in ${SOCKET_DIR}"
}

firewall_hint() {
    if command -v ufw &>/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
        info "ufw is active — open the ports your TCP plugins use, e.g.: sudo ufw allow 5432/tcp"
    fi
    info "Remember your cloud firewall too: TCP 5432 (SNI) and any per-project public port."
}

if already_wired; then
    ensure_dirs
    ok "nginx already includes ${STREAMS_DIR} — nothing else to do"
    firewall_hint
    exit 0
fi

# Another app's stream {} block: nginx refuses a second one, and we never edit theirs.
if grep -qE '^[[:space:]]*stream[[:space:]]*\{' <<<"$DUMP"; then
    warn "nginx already has a stream {} block that freeholdy does not own."
    warn "Add this line inside that block, then re-run this script:"
    echo  "        ${INCLUDE_LINE}"
    exit 2
fi

# ── 3. Stream module available? ────────────────────────────────────────────────
NEED_LOAD_MODULE=""
if grep -q -- '--with-stream=dynamic' <<<"$BUILD"; then
    if grep -qE 'load_module[^;]*ngx_stream_module' <<<"$DUMP"; then
        ok "stream module already loaded"
    else
        MODULES_PATH="$(sed -n 's/.*--modules-path=\([^ ]*\).*/\1/p' <<<"$BUILD")"
        SO=""
        for cand in "${MODULES_PATH:-/nonexistent}/ngx_stream_module.so" \
                    /usr/lib/nginx/modules/ngx_stream_module.so \
                    /usr/share/nginx/modules/ngx_stream_module.so \
                    /etc/nginx/modules/ngx_stream_module.so; do
            [[ -f "$cand" ]] && { SO="$cand"; break; }
        done
        if [[ -z "$SO" ]] && command -v apt-get &>/dev/null; then
            if [[ "$AUTO_INSTALL" -eq 1 ]] || confirm "Install the nginx stream module (apt-get install libnginx-mod-stream)?"; then
                info "apt-get install libnginx-mod-stream"
                DEBIAN_FRONTEND=noninteractive apt-get install -y libnginx-mod-stream >/dev/null
                DUMP="$(nginx -T 2>/dev/null || true)"
            else
                warn "stream module missing. Install it with: sudo apt-get install libnginx-mod-stream"
                warn "then re-run: sudo bash $0"
                exit 2
            fi
        fi
        if grep -qE 'load_module[^;]*ngx_stream_module' <<<"$DUMP"; then
            ok "stream module installed and loaded"
        elif [[ -n "$SO" ]]; then
            NEED_LOAD_MODULE="load_module ${SO};"
        else
            warn "stream module is built as a dynamic module but ngx_stream_module.so was not found — skipping"
            exit 2
        fi
    fi
elif grep -q -- '--with-stream' <<<"$BUILD"; then
    ok "stream module is built in"
else
    warn "this nginx was built without the stream module — skipping stream setup"
    exit 2
fi

# ── 4. Add the stream {} block in nginx's main context ────────────────────────
BACKUP=""
CREATED=()
revert() {
    for f in "${CREATED[@]}"; do rm -f "$f"; done
    if [[ -n "$BACKUP" && -f "$BACKUP" ]]; then cp -p "$BACKUP" "$NGINX_CONF"; fi
    rmdir "${STREAMS_DIR}/sni" "$STREAMS_DIR" 2>/dev/null || true
}

STREAM_BLOCK="# Managed by freeholdy (scripts/setup_nginx_stream.sh) — raw TCP proxies for
# compose services labelled freeholdy.proxy=tcp. Do not edit manually.
stream {
    ${INCLUDE_LINE}
}"

ensure_dirs
if grep -qE "include[[:space:]]+${MODULES_ENABLED}/\*\.conf" <<<"$DUMP" && [[ -z "$NEED_LOAD_MODULE" ]]; then
    # Debian/Ubuntu: modules-enabled is included at the top of nginx.conf (main context);
    # 99- sorts after 50-mod-stream.conf, so the module is loaded before our block.
    printf '%s\n' "$STREAM_BLOCK" > "$OUR_MODULE_CONF"
    CREATED+=("$OUR_MODULE_CONF")
    info "wrote ${OUR_MODULE_CONF}"
else
    BACKUP="${NGINX_CONF}.freeholdy-bak.$(date +%s)"
    cp -p "$NGINX_CONF" "$BACKUP"
    printf '%s\n' "$STREAM_BLOCK" > "$OUR_MAIN_CONF"
    CREATED+=("$OUR_MAIN_CONF")
    {
        echo ""
        echo "# Added by freeholdy (scripts/setup_nginx_stream.sh)"
        echo "include ${OUR_MAIN_CONF};"
    } >> "$NGINX_CONF"
    if [[ -n "$NEED_LOAD_MODULE" ]]; then
        # load_module must precede every block — put it on the first line.
        sed -i "1i ${NEED_LOAD_MODULE}  # added by freeholdy" "$NGINX_CONF"
    fi
    info "appended an include to ${NGINX_CONF} (backup: ${BACKUP})"
fi

# ── 5. Test + reload, or revert ────────────────────────────────────────────────
if nginx -t &>/dev/null; then
    # The config is valid either way; a failed reload only means nginx isn't running
    # right now, and it picks the stream block up on its next start.
    nginx -s reload 2>/dev/null || warn "nginx is not running — the stream block applies when it starts"
    ok "nginx stream support enabled (${STREAMS_DIR})"
    firewall_hint
    exit 0
fi

fail "nginx -t failed after adding the stream block — reverting:"
nginx -t 2>&1 | sed 's/^/      /' >&2 || true
revert
if nginx -t &>/dev/null; then
    ok "reverted — nginx config is back to its previous, valid state"
else
    fail "nginx -t still fails after reverting — check ${NGINX_CONF} (backup: ${BACKUP:-none})"
fi
exit 1
