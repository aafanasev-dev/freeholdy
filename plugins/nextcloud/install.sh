#!/bin/bash
# install.sh — Nextcloud plugin setup.
#
# Interactive compose plugin (plugin.json "interactive": true): the pre phase runs
# attached to a WebSocket session (pty) so it can prompt the user.
#
#   Phase "pre"  (interactive, before docker compose up):
#     Asks for the Nextcloud admin account (the image runs an unattended
#     maintenance:install on first boot when NEXTCLOUD_ADMIN_USER/PASSWORD are
#     set) and writes everything to ${PROJECT_DIR}/.env. POSTGRES_PASSWORD and
#     REDIS_PASSWORD are generated keep-if-present — postgres bakes its password
#     into the db volume at first init, so a pre re-run must never rotate them.
#
#   Phase "post" (background thread, after docker compose up):
#     Polls status.php until Nextcloud reports installed (first boot copies the
#     ~700MB source tree into the app volume and runs the installer — minutes,
#     not seconds), switches background jobs to the cron container via occ,
#     health-checks the containers, and saves credentials to
#     ${PROJECT_DIR}/nextcloud-credentials (mode 600).
#
# Everything lives inside the project dir — the freeholdy service user cannot
# write /var/lib or /etc. User data and the DB live in the named docker volumes
# {project}_app / {project}_db, which SURVIVE project deletion (see ABOUT.md).
#
# Environment variables provided by freeholdy:
#   PLUGIN_DIR    — this plugin's source directory
#   PROJECT_DIR   — compose project directory (contains .env, compose files)
#   PROJECT_NAME  — project name (e.g. "nextcloud")
#   BASE_DOMAIN   — e.g. "your_domain.com"
#   SERVICE_APP_LOCAL_PORT — (post phase only) app's allocated loopback port

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
# (postgres persists its password inside the db volume at first init).
_env_keep() {
    local key="$1"
    grep -q "^${key}=" "${PROJECT_DIR}/.env" 2>/dev/null || _env_set "$key" "$2"
}

# ── Pre phase ──────────────────────────────────────────────────────────────────
if [[ "$PHASE" == "pre" ]]; then
    echo "Nextcloud setup — file sync & share at https://nextcloud.${BASE_DOMAIN}"
    echo ""

    while :; do
        read -r -p "Admin username [admin]: " NC_USER
        NC_USER="${NC_USER:-admin}"
        if [[ "$NC_USER" =~ ^[a-zA-Z0-9._-]+$ ]]; then
            break
        fi
        echo "'${NC_USER}' is not a valid username — use letters, digits, and . _ -"
    done
    read -r -p "Admin password [blank = auto-generate]: " NC_PASS
    NC_PASS="${NC_PASS:-$(_gen_pass)}"

    _env_set BASE_DOMAIN "$BASE_DOMAIN"
    _env_set NEXTCLOUD_ADMIN_USER "$NC_USER"
    _env_set NEXTCLOUD_ADMIN_PASSWORD "$NC_PASS"
    _env_keep POSTGRES_PASSWORD "$(_gen_pass)"
    _env_keep REDIS_PASSWORD "$(_gen_pass)"

    echo ""
    echo "Configured: https://nextcloud.${BASE_DOMAIN}, admin '${NC_USER}'."
    echo "The first boot installs Nextcloud into its data volume — expect several"
    echo "minutes before the site answers. Credentials will be saved to"
    echo "${PROJECT_DIR}/nextcloud-credentials after install."
    exit 0
fi

# ── Post phase ─────────────────────────────────────────────────────────────────

# Load what the pre phase wrote (admin account, generated secrets).
# shellcheck source=/dev/null
set -a; source "${PROJECT_DIR}/.env"; set +a

APP="freeholdy_${PROJECT_NAME}_app"
CREDS_FILE="${PROJECT_DIR}/nextcloud-credentials"

# Wait for the unattended install to finish. compose up is still running when
# this thread starts, so early connection failures are normal — the `until` form
# keeps `set -e` from aborting on them.
echo "nextcloud: waiting for the first-boot install (status.php) — this can take minutes…"
MAX_WAIT=900
WAITED=0
until OUT=$(curl -fsS -H "Host: nextcloud.${BASE_DOMAIN}" \
        "http://127.0.0.1:${SERVICE_APP_LOCAL_PORT}/status.php" 2>/dev/null) \
      && echo "$OUT" | grep -q '"installed":true'; do
    if [[ $WAITED -ge $MAX_WAIT ]]; then
        echo "nextcloud: not installed after ${MAX_WAIT}s. Check: docker logs ${APP}" >&2
        exit 1
    fi
    sleep 5
    WAITED=$(( WAITED + 5 ))
done
echo "nextcloud: installed and answering after ${WAITED}s"

# occ gaps that have no env var. background:cron switches job execution from
# AJAX to the cron container — the container alone isn't enough. Non-fatal:
# both are admin-panel hygiene, not availability.
docker exec -u www-data "$APP" php occ background:cron >/dev/null 2>&1 \
    && echo "nextcloud: background jobs switched to cron" \
    || echo "nextcloud: WARNING — could not run 'occ background:cron'. Run manually: docker exec -u www-data ${APP} php occ background:cron" >&2
docker exec -u www-data "$APP" php occ config:system:set maintenance_window_start --type=integer --value=1 >/dev/null 2>&1 \
    || echo "nextcloud: WARNING — could not set maintenance_window_start." >&2

for SVC in app db cache cron; do
    if [[ "$(docker inspect -f '{{.State.Running}}' "freeholdy_${PROJECT_NAME}_${SVC}" 2>/dev/null || echo false)" != "true" ]]; then
        echo "nextcloud: WARNING — container freeholdy_${PROJECT_NAME}_${SVC} is not running." >&2
    fi
done

cat > "$CREDS_FILE" << CREDS
# Nextcloud credentials — generated by the nextcloud plugin install.sh
# Keep this file secret: chmod 600 $CREDS_FILE

URL=https://nextcloud.${BASE_DOMAIN}
ADMIN_USER=${NEXTCLOUD_ADMIN_USER}
ADMIN_PASS=${NEXTCLOUD_ADMIN_PASSWORD}

# DB/Redis passwords (internal-only services) live in ${PROJECT_DIR}/.env.
CREDS
chmod 600 "$CREDS_FILE"
echo "nextcloud: credentials saved to ${CREDS_FILE}"

echo ""
echo "━━━  Nextcloud plugin setup complete  ━━━"
echo "URL: https://nextcloud.${BASE_DOMAIN}   (admin: ${NEXTCLOUD_ADMIN_USER})"
echo ""
echo "Your files and database live in the docker volumes ${PROJECT_NAME}_app and"
echo "${PROJECT_NAME}_db. They SURVIVE project deletion — to wipe everything run:"
echo "  docker volume rm ${PROJECT_NAME}_app ${PROJECT_NAME}_db"
exit 0
