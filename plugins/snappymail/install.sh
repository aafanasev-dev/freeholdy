#!/bin/bash
# install.sh — SnappyMail plugin setup.
#
# Interactive compose plugin (plugin.json "interactive": true): the pre phase runs
# attached to a WebSocket session (pty) so it can prompt the user.
#
#   Phase "pre"  (interactive, before docker compose up):
#     If ${PROJECT_DIR}/docker-data/snappymail holds data from a previous
#     install attempt, asks whether to keep the old config or recreate it.
#     Then asks for the admin account (username + password; blank password
#     auto-generates one) and stores the answers in ${PROJECT_DIR}/.env.
#
#   Phase "post" (background thread, after docker compose up):
#     Waits for the web UI, applies the chosen admin credentials, drops a
#     domain config pointing IMAP/SMTP at mail.{BASE_DOMAIN} (the mailserver
#     plugin), saves credentials to ${PROJECT_DIR}/snappymail-credentials
#     (mode 600), and prints the webmail + admin URLs.
#
# The data volume is written by the container's user, so the post phase does
# all config work through `docker exec` (the freeholdy service user can't
# touch those files directly — same reason the "recreate" wipe happens in
# post, inside the container, rather than in pre on the host).

set -euo pipefail

PHASE="${1:-post}"

DATA_ROOT="${PROJECT_DIR}/docker-data/snappymail"

# Container-side paths (inside the /var/lib/snappymail volume).
C_DATA="/var/lib/snappymail/_data_"
C_INI="${C_DATA}/_default_/configs/application.ini"
C_DOMAINS="${C_DATA}/_default_/domains"

_gen_pass() {
    openssl rand -hex 12
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

# ── Pre phase ──────────────────────────────────────────────────────────────────
if [[ "$PHASE" == "pre" ]]; then
    RESET=0
    if [[ -d "$DATA_ROOT" ]] && [[ -n "$(ls -A "$DATA_ROOT" 2>/dev/null)" ]]; then
        read -r -p "Existing SnappyMail data found in ${DATA_ROOT} — keep the old config or recreate it? [Keep/recreate]: " ANSWER
        if [[ "${ANSWER,,}" == r* ]]; then
            RESET=1
            echo "Old SnappyMail data will be wiped when the container starts."
        else
            echo "Keeping existing SnappyMail data."
        fi
    else
        echo "No previous SnappyMail data found — fresh install."
    fi

    while :; do
        read -r -p "Admin username [admin]: " ADMIN_USER
        ADMIN_USER="${ADMIN_USER:-admin}"
        if [[ "$ADMIN_USER" =~ ^[A-Za-z0-9._-]+$ ]]; then
            break
        fi
        echo "'${ADMIN_USER}' is not a valid username — use letters, digits, and . _ -"
    done
    read -r -p "Admin password [blank = auto-generate]: " ADMIN_PASS
    ADMIN_PASS="${ADMIN_PASS:-$(_gen_pass)}"

    _env_set SNAPPYMAIL_RESET "$RESET"
    _env_set SNAPPYMAIL_ADMIN_USER "$ADMIN_USER"
    _env_set SNAPPYMAIL_ADMIN_PASS "$ADMIN_PASS"
    echo "Admin account '${ADMIN_USER}' will be configured after the container starts."
    exit 0
fi

# ── Post phase ─────────────────────────────────────────────────────────────────

# Load what the pre phase wrote (SNAPPYMAIL_* — and BASE_DOMAIN comes from the env).
# shellcheck source=/dev/null
set -a; source "${PROJECT_DIR}/.env"; set +a

LOCAL_PORT="${SERVICE_SNAPPYMAIL_LOCAL_PORT}"
CONTAINER="freeholdy_${PROJECT_NAME}_snappymail"
CREDS_FILE="${PROJECT_DIR}/snappymail-credentials"

_wait_ui() {
    local max_wait="$1" waited=0
    until [[ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${LOCAL_PORT}/" 2>/dev/null)" =~ ^[23] ]]; do
        if [[ $waited -ge $max_wait ]]; then
            echo "snappymail: web UI not ready after ${max_wait}s. Check: docker logs ${CONTAINER}" >&2
            exit 1
        fi
        sleep 3
        waited=$(( waited + 3 ))
    done
    echo "snappymail: web UI ready after ${waited}s"
}

echo "snappymail: waiting for the web UI on port ${LOCAL_PORT}…"
_wait_ui 180

# Recreate requested in the pre phase: wipe the data inside the container
# (host-side files are owned by the container's user) and let a restart
# regenerate a pristine config.
if [[ "${SNAPPYMAIL_RESET:-0}" == "1" ]]; then
    echo "snappymail: wiping previous data…"
    docker exec "$CONTAINER" sh -c "rm -rf ${C_DATA}"
    docker restart "$CONTAINER" >/dev/null
    _wait_ui 120
fi

# Wait for the generated application.ini, then set the admin login + password.
# The hash is computed by the container's own PHP (same algorithm SnappyMail
# uses); editing is line-based to avoid regex-backreference issues with bcrypt
# hashes containing "$".
WAITED=0
until docker exec "$CONTAINER" test -s "$C_INI" 2>/dev/null; do
    if [[ $WAITED -ge 60 ]]; then
        echo "snappymail: ${C_INI} did not appear after 60s. Check: docker logs ${CONTAINER}" >&2
        exit 1
    fi
    sleep 3
    WAITED=$(( WAITED + 3 ))
done

docker exec "$CONTAINER" php -r '
$f = $argv[1]; $u = $argv[2];
$hash = password_hash($argv[3], PASSWORD_DEFAULT);
$lines = file($f); $fu = $fp = false;
foreach ($lines as &$l) {
    if (preg_match("/^admin_login\s*=/", $l))    { $l = "admin_login = \"$u\"\n"; $fu = true; }
    if (preg_match("/^admin_password\s*=/", $l)) { $l = "admin_password = \"$hash\"\n"; $fp = true; }
}
unset($l);
$out = implode("", $lines);
if (!$fu || !$fp) {
    $extra = (!$fu ? "admin_login = \"$u\"\n" : "") . (!$fp ? "admin_password = \"$hash\"\n" : "");
    if (strpos($out, "[security]") !== false) {
        $out = str_replace("[security]\n", "[security]\n" . $extra, $out);
    } else {
        $out .= "\n[security]\n" . $extra;
    }
}
file_put_contents($f, $out);
' "$C_INI" "$SNAPPYMAIL_ADMIN_USER" "$SNAPPYMAIL_ADMIN_PASS"
# The random-password file from first start is now stale — remove it.
docker exec "$CONTAINER" rm -f "${C_DATA}/_default_/admin_password.txt"
echo "snappymail: admin account '${SNAPPYMAIL_ADMIN_USER}' configured"

# Point the base domain's IMAP/SMTP at the mailserver plugin so logging in as
# user@{BASE_DOMAIN} just works. Existing file (kept old config) is left alone.
if docker exec "$CONTAINER" test -f "${C_DOMAINS}/${BASE_DOMAIN}.ini" 2>/dev/null; then
    echo "snappymail: domain config for ${BASE_DOMAIN} already exists — keeping it."
else
    docker exec -i "$CONTAINER" sh -c "mkdir -p '${C_DOMAINS}' && cat > '${C_DOMAINS}/${BASE_DOMAIN}.ini'" << INI
imap_host = "mail.${BASE_DOMAIN}"
imap_port = 993
imap_secure = "SSL"
imap_short_login = Off
sieve_use = Off
smtp_host = "mail.${BASE_DOMAIN}"
smtp_port = 465
smtp_secure = "SSL"
smtp_short_login = Off
smtp_auth = On
white_list = ""
INI
    echo "snappymail: domain ${BASE_DOMAIN} configured against mail.${BASE_DOMAIN} (IMAP 993 / SMTP 465)"
fi

# Restart so the new admin credentials and domain config are picked up for sure.
docker restart "$CONTAINER" >/dev/null
_wait_ui 120

cat > "$CREDS_FILE" << CREDS
# SnappyMail credentials — generated by the snappymail plugin install.sh
# Keep this file secret: chmod 600 $CREDS_FILE

ADMIN_URL=https://mailui.${BASE_DOMAIN}/?admin
ADMIN_USER=${SNAPPYMAIL_ADMIN_USER}
ADMIN_PASS=${SNAPPYMAIL_ADMIN_PASS}
CREDS
chmod 600 "$CREDS_FILE"
echo "snappymail: admin credentials saved to ${CREDS_FILE}"

echo ""
echo "━━━  SnappyMail plugin setup complete  ━━━"
echo "  Webmail: https://mailui.${BASE_DOMAIN}  (log in with a full e-mail address"
echo "           and its mailbox password — accounts come from the mailserver plugin)"
echo "  Admin:   https://mailui.${BASE_DOMAIN}/?admin  (user: ${SNAPPYMAIL_ADMIN_USER})"
echo "  Credentials: ${CREDS_FILE}"
exit 0
