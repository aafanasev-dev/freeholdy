#!/bin/bash
# install.sh — PostgreSQL plugin setup.
#
# Interactive compose plugin (plugin.json "interactive": true): the pre phase runs on a
# pty attached to the install WebSocket, so it can prompt.
#
#   Phase "pre"  (interactive, before docker compose up):
#     Asks for the database, user and password, and HOW clients connect:
#       sni  — {project}.{BASE_DOMAIN}:5432, TLS ended by nginx with the Let's Encrypt
#              cert, routed by hostname (needs libpq/psql 17+ with sslnegotiation=direct)
#       port — {project}.{BASE_DOMAIN}:{public port}, a plain TCP passthrough; postgres
#              does its own TLS with a self-signed cert (works with every client)
#       both
#     Creates the data folder {DATA_DIR}/postgresql/{project}, a self-signed cert in
#     ${PROJECT_DIR}/tls, writes ${PROJECT_DIR}/.env (compose interpolation) and
#     ${PROJECT_DIR}/nginx-stream.json (read by freeholdy when it writes nginx's config).
#
#   Phase "post" (background thread, after docker compose up):
#     Waits for the server to accept connections and saves ready-to-paste connection
#     strings to ${PROJECT_DIR}/postgresql-credentials (mode 600).
#
# Environment variables provided by freeholdy:
#   PLUGIN_DIR, PROJECT_DIR, PROJECT_NAME, PROJECTS_DIR, BASE_DOMAIN, DATA_DIR,
#   FREEHOLDY_PORT, PORT_RANGE_START, PORT_RANGE_END, NGINX_STREAMS_DIR, STREAM_SNI_PORT

set -euo pipefail

PHASE="${1:-post}"
HOST="${PROJECT_NAME}.${BASE_DOMAIN}"
SNI_PORT="${STREAM_SNI_PORT:-5432}"
STREAMS_DIR="${NGINX_STREAMS_DIR:-/etc/nginx/freeholdy-streams}"
ENV_FILE="${PROJECT_DIR}/.env"
STREAM_JSON="${PROJECT_DIR}/nginx-stream.json"
CREDS_FILE="${PROJECT_DIR}/postgresql-credentials"

# 32 hex chars. (openssl, not a `tr </dev/urandom | head` pipe — under pipefail that
# dies with SIGPIPE and `set -e` silently kills the whole install.)
_gen_pass() {
    openssl rand -hex 16
}

# Idempotent KEY=VALUE write to the project .env (drop any previous line first).
_env_set() {
    local key="$1" value="$2"
    touch "$ENV_FILE"
    grep -v "^${key}=" "$ENV_FILE" > "${ENV_FILE}.tmp" || true
    mv "${ENV_FILE}.tmp" "$ENV_FILE"
    printf "%s='%s'\n" "$key" "$value" >> "$ENV_FILE"
}

# Current value of KEY in the project .env ("" when absent).
_env_get() {
    local key="$1" line
    line="$(grep -m1 "^${key}=" "$ENV_FILE" 2>/dev/null || true)"
    line="${line#*=}"
    line="${line#\'}"
    printf '%s' "${line%\'}"
}

# Is TCP port $1 listening on this host?
_port_listening() {
    ss -ltnH 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$1\$"
}

# Project name claiming public_port $1 in another project's nginx-stream.json ("" = none).
_port_claimed_by() {
    local want="$1" f other
    for f in "${PROJECTS_DIR}"/*/nginx-stream.json; do
        [[ -f "$f" ]] || continue
        other="$(basename "$(dirname "$f")")"
        [[ "$other" == "$PROJECT_NAME" ]] && continue
        if python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if str(d.get("public_port") or "")==sys.argv[2] else 1)' "$f" "$want" 2>/dev/null; then
            echo "$other"
            return 0
        fi
    done
    return 0
}

# Why public port $1 can't be used ("" = it can).
_port_problem() {
    local p="$1" mode="$2" owner
    [[ "$p" =~ ^[0-9]+$ ]] || { echo "not a number"; return; }
    if (( p < 1024 || p > 65535 )); then echo "must be within 1024-65535"; return; fi
    if (( p >= PORT_RANGE_START && p < PORT_RANGE_END )); then
        echo "inside freeholdy's container port range ${PORT_RANGE_START}-$((PORT_RANGE_END - 1))"; return
    fi
    if [[ "$p" == "${FREEHOLDY_PORT:-}" || "$p" == 80 || "$p" == 443 ]]; then echo "reserved"; return; fi
    if [[ "$p" == "$SNI_PORT" ]]; then echo "the shared SNI port — pick another one"; return; fi
    owner="$(_port_claimed_by "$p")"
    if [[ -n "$owner" ]]; then echo "already used by project '${owner}'"; return; fi
    if _port_listening "$p"; then echo "something on this host already listens on it"; return; fi
    echo ""
}

# ── Pre phase ──────────────────────────────────────────────────────────────────
if [[ "$PHASE" == "pre" ]]; then
    echo "PostgreSQL setup — database server at ${HOST}"
    echo ""

    if [[ ! -d "$STREAMS_DIR" ]]; then
        echo "nginx stream support is not installed on this server, so a database cannot"
        echo "be exposed through nginx yet. As root, run:"
        echo "    sudo bash <freeholdy dir>/scripts/setup_nginx_stream.sh"
        echo "then add the plugin again."
        exit 1
    fi
    touch "$ENV_FILE"

    # ── Data folder: detect a cluster left by an earlier install with this name ──
    PG_DATA_DIR="${DATA_DIR}/postgresql/${PROJECT_NAME}"
    # ctl.sh's credential JSONs (/home/projects in the container). A sibling tree rather
    # than postgresql/{project}-projects, which a project named "{project}-projects" owns.
    PG_PROJECTS_DIR="${DATA_DIR}/postgresql-projects/${PROJECT_NAME}"
    KEEP_CLUSTER=0
    # The postgres entrypoint chowns the folder to its own uid on first start, so a
    # folder not owned by us is an initialised cluster.
    if [[ -d "$PG_DATA_DIR" && "$(stat -c %u "$PG_DATA_DIR")" != "$(id -u)" ]]; then
        echo "An existing PostgreSQL data folder was found at:"
        echo "    ${PG_DATA_DIR}"
        echo "(left by an earlier '${PROJECT_NAME}' install — deleting a project keeps it)."
        while :; do
            read -r -p "[k]eep it and its databases, [w]ipe it, or [a]bort? [k]: " CHOICE
            case "${CHOICE:-k}" in
                k|K) KEEP_CLUSTER=1; break ;;
                w|W)
                    read -r -p "Type the project name '${PROJECT_NAME}' to confirm wiping it: " CONFIRM
                    if [[ "$CONFIRM" == "$PROJECT_NAME" ]]; then
                        docker run --rm -v "${PG_DATA_DIR}:/d" alpine \
                            sh -c 'find /d -mindepth 1 -delete && chown '"$(id -u):$(id -g)"' /d'
                        # ctl.sh's credential files belong to the wiped cluster too.
                        if [[ -d "$PG_PROJECTS_DIR" ]]; then
                            docker run --rm -v "${PG_PROJECTS_DIR}:/d" alpine \
                                sh -c 'find /d -mindepth 1 -delete'
                        fi
                        echo "Wiped."
                        break
                    fi
                    echo "Not confirmed." ;;
                a|A) echo "Aborted."; exit 1 ;;
                *) echo "Please answer k, w or a." ;;
            esac
        done
        echo ""
    fi
    mkdir -p "$PG_DATA_DIR" "$PG_PROJECTS_DIR"

    # ── Database / user / password ──
    DEFAULT_IDENT="${PROJECT_NAME//-/_}"
    if [[ "$KEEP_CLUSTER" -eq 1 ]]; then
        echo "The kept cluster still has the user and password it was created with — enter"
        echo "them so the saved connection strings are right (nothing is changed in it)."
    fi
    while :; do
        CUR="$(_env_get PG_DB)"; CUR="${CUR:-$DEFAULT_IDENT}"
        read -r -p "Database name [${CUR}]: " PG_DB
        PG_DB="${PG_DB:-$CUR}"
        [[ "$PG_DB" =~ ^[a-z_][a-z0-9_]{0,62}$ ]] && break
        echo "'${PG_DB}' is not valid — lowercase letters, digits and _, not starting with a digit."
    done
    while :; do
        CUR="$(_env_get PG_USER)"; CUR="${CUR:-$DEFAULT_IDENT}"
        read -r -p "Database user [${CUR}]: " PG_USER
        PG_USER="${PG_USER:-$CUR}"
        [[ "$PG_USER" =~ ^[a-z_][a-z0-9_]{0,62}$ ]] && break
        echo "'${PG_USER}' is not valid — lowercase letters, digits and _, not starting with a digit."
    done
    BAD_PASS_CHARS='[[:space:]'"'"'"\$`]'
    while :; do
        CUR="$(_env_get PG_PASSWORD)"
        if [[ -n "$CUR" ]]; then
            read -r -p "Password [blank = keep the current one]: " PG_PASSWORD
            PG_PASSWORD="${PG_PASSWORD:-$CUR}"
        else
            read -r -p "Password [blank = auto-generate]: " PG_PASSWORD
            PG_PASSWORD="${PG_PASSWORD:-$(_gen_pass)}"
        fi
        # Kept out of the value: quotes, backslash, $ and whitespace would need escaping in
        # compose's .env interpolation and in connection URLs.
        if [[ ${#PG_PASSWORD} -ge 8 && ! "$PG_PASSWORD" =~ $BAD_PASS_CHARS ]]; then
            break
        fi
        echo "Use at least 8 characters, without spaces, quotes, backslashes, \$ or backticks."
    done
    echo ""

    # ── Connection mode ──
    # 5432 held by something that is not freeholdy's SNI listener rules SNI out.
    SNI_BLOCKED=""
    if _port_listening "$SNI_PORT" && ! compgen -G "${STREAMS_DIR}/sni/*.map" >/dev/null; then
        SNI_BLOCKED="port ${SNI_PORT} is already used by another program on this host"
    fi
    echo "How should clients connect?"
    echo "  sni  — ${HOST}:${SNI_PORT}, routed by hostname, Let's Encrypt TLS."
    echo "         Needs libpq/psql 17+ and sslnegotiation=direct in the connection string."
    echo "  port — ${HOST}:<a port of its own>, any PostgreSQL client (sslmode=require)."
    echo "  both — both of the above."
    [[ -n "$SNI_BLOCKED" ]] && echo "  (sni is unavailable: ${SNI_BLOCKED})"
    DEFAULT_MODE="sni"; [[ -n "$SNI_BLOCKED" ]] && DEFAULT_MODE="port"
    while :; do
        read -r -p "Connection mode [sni/port/both] [${DEFAULT_MODE}]: " MODE
        MODE="${MODE:-$DEFAULT_MODE}"
        case "$MODE" in
            sni|both)
                if [[ -n "$SNI_BLOCKED" ]]; then echo "sni is unavailable: ${SNI_BLOCKED}"; continue; fi
                break ;;
            port) break ;;
            *) echo "Please answer sni, port or both." ;;
        esac
    done

    PUBLIC_PORT=""
    if [[ "$MODE" == "port" || "$MODE" == "both" ]]; then
        SUGGEST=15432
        while [[ -n "$(_port_problem "$SUGGEST" "$MODE")" && "$SUGGEST" -lt 15532 ]]; do
            SUGGEST=$((SUGGEST + 1))
        done
        while :; do
            read -r -p "Public TCP port [${SUGGEST}]: " PUBLIC_PORT
            PUBLIC_PORT="${PUBLIC_PORT:-$SUGGEST}"
            PROBLEM="$(_port_problem "$PUBLIC_PORT" "$MODE")"
            [[ -z "$PROBLEM" ]] && break
            echo "Port ${PUBLIC_PORT}: ${PROBLEM}."
        done
    fi
    echo ""

    # ── Self-signed cert for postgres's own TLS (port mode; harmless otherwise) ──
    mkdir -p "${PROJECT_DIR}/tls"
    if [[ ! -s "${PROJECT_DIR}/tls/server.key" || ! -s "${PROJECT_DIR}/tls/server.crt" ]]; then
        openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
            -subj "/CN=${HOST}" -addext "subjectAltName=DNS:${HOST}" \
            -keyout "${PROJECT_DIR}/tls/server.key" -out "${PROJECT_DIR}/tls/server.crt" \
            >/dev/null 2>&1
        echo "Generated a self-signed TLS certificate for ${HOST} (valid 10 years)."
    fi
    # The container's root reads the key regardless of mode and copies it into place
    # (0600, owned by postgres) — on the host it stays private to the service user.
    chmod 644 "${PROJECT_DIR}/tls/server.crt"
    chmod 600 "${PROJECT_DIR}/tls/server.key"

    _env_set BASE_DOMAIN "$BASE_DOMAIN"
    _env_set PG_DB "$PG_DB"
    _env_set PG_USER "$PG_USER"
    _env_set PG_PASSWORD "$PG_PASSWORD"
    _env_set PG_DATA_DIR "$PG_DATA_DIR"
    _env_set PG_MODE "$MODE"
    _env_set PG_PUBLIC_PORT "$PUBLIC_PORT"
    _env_set PG_PROJECTS_DIR "$PG_PROJECTS_DIR"
    _env_set PG_HOST "$HOST"
    PG_SNI_PORT=""; [[ "$MODE" == "sni" || "$MODE" == "both" ]] && PG_SNI_PORT="$SNI_PORT"
    _env_set PG_SNI_PORT "$PG_SNI_PORT"
    chmod 600 "$ENV_FILE"

    SNI_JSON=false; [[ "$MODE" == "sni" || "$MODE" == "both" ]] && SNI_JSON=true
    PORT_JSON=null; [[ -n "$PUBLIC_PORT" ]] && PORT_JSON="$PUBLIC_PORT"
    printf '{"sni": %s, "public_port": %s, "alpn": "postgresql"}\n' "$SNI_JSON" "$PORT_JSON" > "$STREAM_JSON"

    echo "Configured: database '${PG_DB}', user '${PG_USER}', mode '${MODE}'."
    [[ "$SNI_JSON" == true ]] && echo "  SNI : ${HOST}:${SNI_PORT}"
    [[ -n "$PUBLIC_PORT" ]]  && echo "  Port: ${HOST}:${PUBLIC_PORT}"
    echo "Data folder: ${PG_DATA_DIR}"
    echo "App databases: docker exec -it freeholdy_${PROJECT_NAME}_db /home/ctl.sh add myapp"
    echo "Connection strings will be saved to ${CREDS_FILE}."
    exit 0
fi

# ── Post phase ─────────────────────────────────────────────────────────────────

PG_DB="$(_env_get PG_DB)"
PG_USER="$(_env_get PG_USER)"
PG_PASSWORD="$(_env_get PG_PASSWORD)"
PG_DATA_DIR="$(_env_get PG_DATA_DIR)"
MODE="$(_env_get PG_MODE)"
PUBLIC_PORT="$(_env_get PG_PUBLIC_PORT)"
DB_CONTAINER="freeholdy_${PROJECT_NAME}_db"

# compose up is still running when this thread starts — early failures are normal.
echo "postgresql: waiting for the server to accept connections…"
MAX_WAIT=180
WAITED=0
until docker exec "$DB_CONTAINER" pg_isready -q -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; do
    if [[ $WAITED -ge $MAX_WAIT ]]; then
        echo "postgresql: not ready after ${MAX_WAIT}s. Check: docker logs ${DB_CONTAINER}" >&2
        exit 1
    fi
    sleep 3
    WAITED=$(( WAITED + 3 ))
done
echo "postgresql: ready after ${WAITED}s"

ENC_PASS="$(python3 -I -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$PG_PASSWORD")"

{
    echo "# PostgreSQL connection details — generated by the postgresql plugin install.sh"
    echo "# Keep this file secret (mode 600)."
    echo ""
    echo "HOST=${HOST}"
    echo "DATABASE=${PG_DB}"
    echo "USER=${PG_USER}"
    echo "PASSWORD=${PG_PASSWORD}"
    echo "DATA_DIR=${PG_DATA_DIR}"
    echo ""
    if [[ "$MODE" == "sni" || "$MODE" == "both" ]]; then
        echo "# By hostname on the shared port — libpq/psql 17+ only. TLS uses the Let's Encrypt"
        echo "# certificate, so it works once DNS for ${HOST} points at this server."
        echo "SNI_URL=postgresql://${PG_USER}:${ENC_PASS}@${HOST}:${SNI_PORT}/${PG_DB}?sslmode=verify-full&sslnegotiation=direct&sslrootcert=system"
        echo ""
    fi
    if [[ -n "$PUBLIC_PORT" ]]; then
        echo "# On the dedicated port — any client. The server certificate is self-signed:"
        echo "# sslmode=require encrypts; for verify-full, give clients ${PROJECT_DIR}/tls/server.crt."
        echo "PORT_URL=postgresql://${PG_USER}:${ENC_PASS}@${HOST}:${PUBLIC_PORT}/${PG_DB}?sslmode=require"
        echo ""
    fi
    echo "# A psql shell inside the container, on the server:"
    echo "#   docker exec -it ${DB_CONTAINER} psql -U ${PG_USER} -d ${PG_DB}"
} > "$CREDS_FILE"
chmod 600 "$CREDS_FILE"
echo "postgresql: connection details saved to ${CREDS_FILE}"

echo ""
echo "━━━  PostgreSQL plugin setup complete  ━━━"
if [[ "$MODE" == "sni" || "$MODE" == "both" ]]; then
    echo "SNI : psql \"host=${HOST} port=${SNI_PORT} dbname=${PG_DB} user=${PG_USER} sslmode=verify-full sslnegotiation=direct sslrootcert=system\""
fi
if [[ -n "$PUBLIC_PORT" ]]; then
    echo "Port: psql \"host=${HOST} port=${PUBLIC_PORT} dbname=${PG_DB} user=${PG_USER} sslmode=require\""
fi
echo ""
FW_PORTS=()
[[ "$MODE" == "sni" || "$MODE" == "both" ]] && FW_PORTS+=( "$SNI_PORT" )
[[ -n "$PUBLIC_PORT" ]] && FW_PORTS+=( "$PUBLIC_PORT" )
echo "Open TCP port(s) ${FW_PORTS[*]} in your firewall (cloud firewall and ufw, if any)."
echo "Data lives in ${PG_DATA_DIR} and is KEPT when the project is deleted."
echo ""
echo "One database + user per app (credentials land in /home/projects/<name>.json):"
echo "  docker exec -it ${DB_CONTAINER} /home/ctl.sh add myapp      # also: list, remove myapp"
exit 0
