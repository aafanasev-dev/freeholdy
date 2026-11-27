#!/bin/bash
# ctl.sh — one database + one user per app, on this PostgreSQL server.
#
# Installed into the container as /home/ctl.sh by the compose entrypoint. Run it there:
#     docker exec -it freeholdy_<project>_db /home/ctl.sh add myapp
# (or from the web UI's exec on the `db` service: cd /home && ./ctl.sh list).
#
#   add NAME     create database NAME + login user NAME that can connect to NAME only;
#                credentials go to /home/projects/NAME.json (mode 600)
#   remove NAME  drop that database (open sessions are terminated), the user, and the JSON
#   list         login users with the databases they own
#
# /home/projects is a host folder ({DATA_DIR}/postgresql-projects/<project>), so the JSON
# files survive container restarts and redeploys. NAME may contain '-', which becomes '_'
# in the database/user name (SQL identifiers); the JSON keeps NAME as given.
#
# Talks to the server as the superuser the cluster was created with ($POSTGRES_USER) over
# the local socket. FH_PG_HOST / FH_PG_SNI_PORT / FH_PG_PUBLIC_PORT (set by the plugin's
# compose file) say how clients reach this server from outside.

set -euo pipefail

PROJECTS_DIR=/home/projects
SU="${POSTGRES_USER:-postgres}"
MAIN_DB="${POSTGRES_DB:-$SU}"
export PGPASSWORD="${POSTGRES_PASSWORD:-}"   # local auth is trust; this is a fallback

usage() {
    cat <<EOF
usage: $(basename "$0") add NAME | remove NAME | list

  add NAME     create database + user NAME (access to that database only),
               save credentials to ${PROJECTS_DIR}/NAME.json
  remove NAME  drop database NAME, user NAME and ${PROJECTS_DIR}/NAME.json
  list         show users and the databases they own

NAME: lowercase letters, digits, '-' and '_', starting with a letter or '_'.
EOF
}

die() { echo "error: $*" >&2; exit 1; }

# Run SQL against database $1 (default: the main one), unaligned + tuples only.
sql() {
    local db="$MAIN_DB"
    if [[ $# -gt 1 ]]; then db="$1"; shift; fi
    psql -X -q -v ON_ERROR_STOP=1 -h /var/run/postgresql -U "$SU" -d "$db" -tA -c "$1"
}

# Validate NAME and derive the SQL identifier (sets NAME and IDENT).
parse_name() {
    [[ $# -ge 1 && -n "$1" ]] || { usage >&2; exit 2; }
    NAME="$1"
    [[ "$NAME" =~ ^[a-z_][a-z0-9_-]{0,62}$ ]] \
        || die "'$NAME' is not a valid name — lowercase letters, digits, '-' and '_', starting with a letter or '_' (max 63)"
    IDENT="${NAME//-/_}"
    if [[ "$IDENT" == "$MAIN_DB" || "$IDENT" == "$SU" || "$IDENT" == postgres || "$IDENT" == template0 || "$IDENT" == template1 ]]; then
        die "'$IDENT' is reserved on this server (its own database/user)"
    fi
}

role_exists() { [[ "$(sql "SELECT 1 FROM pg_roles WHERE rolname = '$1'")" == 1 ]]; }
db_exists()   { [[ "$(sql "SELECT 1 FROM pg_database WHERE datname = '$1'")" == 1 ]]; }

gen_pass() { od -An -tx1 -N16 /dev/urandom | tr -d ' \n'; }

cmd_add() {
    parse_name "$@"
    role_exists "$IDENT" && die "user '$IDENT' already exists (see: $(basename "$0") list)"
    db_exists "$IDENT"   && die "database '$IDENT' already exists (see: $(basename "$0") list)"

    local pass created
    pass="$(gen_pass)"
    created="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    sql "CREATE ROLE \"$IDENT\" LOGIN PASSWORD '$pass'"
    sql "COMMENT ON ROLE \"$IDENT\" IS 'managed by ctl.sh'"
    if ! sql "CREATE DATABASE \"$IDENT\" OWNER \"$IDENT\""; then
        sql "DROP ROLE IF EXISTS \"$IDENT\"" || true
        die "could not create database '$IDENT'"
    fi

    # Every database grants CONNECT to PUBLIC by default, so a new user could log into the
    # others. Take that away everywhere (the superuser and owners are unaffected), then
    # grant the new user its own database. Run on each add: it also covers databases
    # created since the last one.
    local d
    while IFS= read -r d; do
        [[ -n "$d" ]] && sql "REVOKE CONNECT ON DATABASE \"$d\" FROM PUBLIC"
    done < <(sql "SELECT datname FROM pg_database WHERE datallowconn")
    sql "GRANT CONNECT ON DATABASE \"$IDENT\" TO \"$IDENT\""
    sql "$IDENT" "REVOKE ALL ON SCHEMA public FROM PUBLIC; GRANT ALL ON SCHEMA public TO \"$IDENT\""

    local host="${FH_PG_HOST:-}" sni_port="${FH_PG_SNI_PORT:-}" pub_port="${FH_PG_PUBLIC_PORT:-}"
    local sni_url="" port_url=""
    if [[ -n "$host" && -n "$sni_port" ]]; then
        sni_url="postgresql://${IDENT}:${pass}@${host}:${sni_port}/${IDENT}?sslmode=verify-full&sslnegotiation=direct&sslrootcert=system"
    fi
    if [[ -n "$host" && -n "$pub_port" ]]; then
        port_url="postgresql://${IDENT}:${pass}@${host}:${pub_port}/${IDENT}?sslmode=require"
    fi

    mkdir -p "$PROJECTS_DIR"
    local file="${PROJECTS_DIR}/${NAME}.json" tmp
    tmp="$(mktemp "${PROJECTS_DIR}/.${NAME}.XXXXXX")"
    chmod 600 "$tmp"
    {
        printf '{\n'
        printf '  "project": "%s",\n' "$NAME"
        printf '  "database": "%s",\n' "$IDENT"
        printf '  "user": "%s",\n' "$IDENT"
        printf '  "password": "%s",\n' "$pass"
        printf '  "host": "%s",\n' "$host"
        printf '  "sni_port": %s,\n' "${sni_port:-null}"
        printf '  "public_port": %s,\n' "${pub_port:-null}"
        printf '  "sni_url": %s,\n' "$([[ -n "$sni_url" ]] && printf '"%s"' "$sni_url" || printf null)"
        printf '  "port_url": %s,\n' "$([[ -n "$port_url" ]] && printf '"%s"' "$port_url" || printf null)"
        printf '  "internal_url": "postgresql://%s:%s@localhost:5432/%s",\n' "$IDENT" "$pass" "$IDENT"
        printf '  "created_at": "%s"\n' "$created"
        printf '}\n'
    } > "$tmp"
    mv "$tmp" "$file"

    echo "Created database '$IDENT' and user '$IDENT' (access to '$IDENT' only)."
    echo "Credentials: $file"
    [[ -n "$sni_url" ]]  && echo "  sni : $sni_url"
    [[ -n "$port_url" ]] && echo "  port: $port_url"
    return 0
}

cmd_remove() {
    parse_name "$@"
    local file="${PROJECTS_DIR}/${NAME}.json" did=0
    if db_exists "$IDENT"; then
        sql "DROP DATABASE \"$IDENT\" WITH (FORCE)"
        echo "Dropped database '$IDENT'."; did=1
    fi
    if role_exists "$IDENT"; then
        # Objects the user owns in other databases would block DROP ROLE; it can only
        # connect to its own database, which is gone now, so this is normally a no-op.
        sql "DROP ROLE \"$IDENT\"" || die "user '$IDENT' still owns objects elsewhere — reassign or drop them first"
        echo "Dropped user '$IDENT'."; did=1
    fi
    if [[ -f "$file" ]]; then
        rm -f "$file"
        echo "Removed $file."; did=1
    fi
    [[ $did -eq 1 ]] || die "nothing named '$NAME' found"
}

cmd_list() {
    local rows
    rows="$(sql "
        SELECT r.rolname,
               COALESCE((SELECT string_agg(d.datname, ', ' ORDER BY d.datname)
                         FROM pg_database d WHERE d.datdba = r.oid), ''),
               COALESCE(shobj_description(r.oid, 'pg_authid'), '')
        FROM pg_roles r
        WHERE r.rolcanlogin AND NOT r.rolsuper AND r.rolname NOT LIKE 'pg\\_%'
        ORDER BY r.rolname")"
    if [[ -z "$rows" ]]; then
        echo "No users yet (besides the server's own '$SU'). Add one: $(basename "$0") add NAME"
        return 0
    fi
    printf '%-24s %-30s %-24s %s\n' USER DATABASES CREDENTIALS NOTE
    local user dbs note creds f
    while IFS='|' read -r user dbs note; do
        creds="-"
        for f in "${PROJECTS_DIR}/${user}.json" "${PROJECTS_DIR}/${user//_/-}.json"; do
            [[ -f "$f" ]] && { creds="$(basename "$f")"; break; }
        done
        printf '%-24s %-30s %-24s %s\n' "$user" "${dbs:--}" "$creds" "$note"
    done <<< "$rows"
}

case "${1:-}" in
    add)    shift; cmd_add "$@" ;;
    remove) shift; cmd_remove "$@" ;;
    list)   cmd_list ;;
    -h|--help|help) usage ;;
    *) usage >&2; exit 2 ;;
esac
