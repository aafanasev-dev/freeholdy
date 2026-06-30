#!/bin/bash
# install.sh — docker-mailserver plugin setup.
#
# Interactive compose plugin (plugin.json "interactive": true): the pre phase runs
# attached to a WebSocket session (pty) so it can prompt the user.
#
#   Phase "pre"  (interactive, before docker compose up):
#     Asks which addons to enable (all default to no) and for the first mailbox,
#     writes the toggles to ${PROJECT_DIR}/.env, and seeds the account into
#     ${PROJECT_DIR}/docker-data/dms/config/postfix-accounts.cf — docker-mailserver
#     refuses to start with zero accounts, so the account must exist before
#     the container's first start.
#
#   Phase "post" (background thread, after docker compose up):
#     Waits for the container to become healthy, generates DKIM keys, saves
#     credentials to ${PROJECT_DIR}/mailserver-credentials (mode 600), and prints
#     the DNS records (MX, SPF, DKIM, DMARC) that must be added manually.
#
# Everything lives inside the project dir — the freeholdy service user cannot
# write /var/lib or /etc, and DELETE /projects/{name} tears it all down together.
#
# Environment variables provided by freeholdy:
#   PLUGIN_DIR    — this plugin's source directory
#   PROJECT_DIR   — compose project directory (contains .env, compose files)
#   PROJECT_NAME  — project name (e.g. "mail")
#   BASE_DOMAIN   — e.g. "your_domain.com"

set -euo pipefail

PHASE="${1:-post}"

DATA_ROOT="${PROJECT_DIR}/docker-data/dms"
CONFIG_DIR="${DATA_ROOT}/config"
ACCOUNTS_FILE="${CONFIG_DIR}/postfix-accounts.cf"

# 24 hex chars. (Not the `tr </dev/urandom | head` pattern — under pipefail that
# dies with SIGPIPE and `set -e` silently kills the whole install.)
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

_ask_yn() {
    local answer
    read -r -p "$1 [y/N]: " answer
    [[ "${answer,,}" == y* ]] && echo 1 || echo 0
}

# ── Pre phase ──────────────────────────────────────────────────────────────────
if [[ "$PHASE" == "pre" ]]; then
    echo "docker-mailserver setup — addons (all default to no):"
    RSPAMD=$(_ask_yn "Enable Rspamd spam filtering? (~0.5 GB extra RAM)")
    CLAMAV=$(_ask_yn "Enable ClamAV antivirus? (~1.5 GB extra RAM)")
    FAIL2BAN=$(_ask_yn "Enable Fail2ban brute-force protection?")

    _env_set BASE_DOMAIN "$BASE_DOMAIN"
    _env_set ENABLE_RSPAMD "$RSPAMD"
    _env_set ENABLE_CLAMAV "$CLAMAV"
    _env_set ENABLE_FAIL2BAN "$FAIL2BAN"
    # Rspamd replaces the legacy filter chain (per docker-mailserver docs).
    if [[ "$RSPAMD" == "1" ]]; then
        _env_set ENABLE_OPENDKIM 0
        _env_set ENABLE_OPENDMARC 0
        _env_set ENABLE_POLICYD_SPF 0
        _env_set ENABLE_AMAVIS 0
    else
        _env_set ENABLE_OPENDKIM 1
        _env_set ENABLE_OPENDMARC 1
        _env_set ENABLE_POLICYD_SPF 1
        _env_set ENABLE_AMAVIS 1
    fi

    if [[ -s "$ACCOUNTS_FILE" ]]; then
        echo "Existing mail accounts found in ${ACCOUNTS_FILE} — keeping them."
        _env_set MAIL_ACCOUNT ""
        _env_set MAIL_PASS ""
    else
        while :; do
            read -r -p "First mailbox name [postmaster] (@${BASE_DOMAIN} is added automatically): " MAIL_NAME
            MAIL_NAME="${MAIL_NAME:-postmaster}"
            MAIL_NAME="${MAIL_NAME%%@*}"   # tolerate a full address — keep the local part
            if [[ "$MAIL_NAME" =~ ^[A-Za-z0-9._%+-]+$ ]]; then
                break
            fi
            echo "'${MAIL_NAME}' is not a valid mailbox name — use letters, digits, and . _ % + -"
        done
        MAIL_ACCOUNT="${MAIL_NAME}@${BASE_DOMAIN}"
        echo "Mailbox address: ${MAIL_ACCOUNT}"
        read -r -p "Password [blank = auto-generate]: " MAIL_PASS
        MAIL_PASS="${MAIL_PASS:-$(_gen_pass)}"

        mkdir -p "$CONFIG_DIR"
        HASH=$(openssl passwd -6 "$MAIL_PASS")
        printf '%s|{SHA512-CRYPT}%s\n' "$MAIL_ACCOUNT" "$HASH" > "$ACCOUNTS_FILE"
        chmod 600 "$ACCOUNTS_FILE"
        _env_set MAIL_ACCOUNT "$MAIL_ACCOUNT"
        _env_set MAIL_PASS "$MAIL_PASS"
        echo "Mailbox ${MAIL_ACCOUNT} seeded."
    fi
    exit 0
fi

# ── Post phase ─────────────────────────────────────────────────────────────────

# Load what the pre phase wrote (BASE_DOMAIN, MAIL_ACCOUNT, MAIL_PASS, toggles).
# shellcheck source=/dev/null
set -a; source "${PROJECT_DIR}/.env"; set +a

CONTAINER="freeholdy_${PROJECT_NAME}_mailserver"
CREDS_FILE="${PROJECT_DIR}/mailserver-credentials"

echo "mailserver: waiting for ${CONTAINER} to become healthy…"
MAX_WAIT=600
WAITED=0
until [[ "$(docker inspect -f '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo none)" == "healthy" ]]; do
    if [[ $WAITED -ge $MAX_WAIT ]]; then
        echo "mailserver: container not healthy after ${MAX_WAIT}s. Check: docker logs ${CONTAINER}" >&2
        exit 1
    fi
    sleep 5
    WAITED=$(( WAITED + 5 ))
done
echo "mailserver: healthy after ${WAITED}s"

# DKIM keys — idempotent (setup keeps existing keys). Generated for OpenDKIM or
# Rspamd depending on the addon choice; the change-detector picks them up.
if ! docker exec "$CONTAINER" setup config dkim; then
    echo "mailserver: DKIM generation failed — run 'docker exec ${CONTAINER} setup config dkim' manually" >&2
fi

# Save credentials (root-readable only) when this install created the mailbox.
if [[ -n "${MAIL_ACCOUNT:-}" ]]; then
    cat > "$CREDS_FILE" << CREDS
# docker-mailserver credentials — generated by the mailserver plugin install.sh
# Keep this file secret: chmod 600 $CREDS_FILE

MAIL_ACCOUNT=${MAIL_ACCOUNT}
MAIL_PASS=${MAIL_PASS}

IMAP_HOST=mail.${BASE_DOMAIN}
IMAP_PORT=993 (SSL/TLS)
SMTP_HOST=mail.${BASE_DOMAIN}
SMTP_PORT=587 (STARTTLS) or 465 (SSL/TLS)
CREDS
    chmod 600 "$CREDS_FILE"
    echo "mailserver: credentials saved to ${CREDS_FILE}"
fi

DKIM_TXT=$(find "$CONFIG_DIR" -path '*dkim*' -name '*.txt' -exec cat {} + 2>/dev/null || true)

echo ""
echo "━━━  docker-mailserver plugin setup complete  ━━━"
echo "Add these DNS records for ${BASE_DOMAIN}:"
echo "  MX    ${BASE_DOMAIN}.           10 mail.${BASE_DOMAIN}."
echo "  TXT   ${BASE_DOMAIN}.           \"v=spf1 mx ~all\""
echo "  TXT   _dmarc.${BASE_DOMAIN}.    \"v=DMARC1; p=none; rua=mailto:${MAIL_ACCOUNT:-postmaster@${BASE_DOMAIN}}\""
if [[ -n "$DKIM_TXT" ]]; then
    echo "  DKIM record (host mail._domainkey.${BASE_DOMAIN}):"
    echo "$DKIM_TXT"
else
    echo "  DKIM: run 'docker exec ${CONTAINER} setup config dkim' and add the printed record"
fi
echo ""
echo "Also set this VPS's PTR (reverse DNS) to mail.${BASE_DOMAIN} and make sure"
echo "your provider allows outbound port 25, or outgoing mail will not deliver."
if [[ -n "${MAIL_ACCOUNT:-}" ]]; then
    echo "Credentials: ${CREDS_FILE}"
fi
exit 0
