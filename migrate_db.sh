#!/usr/bin/env bash
#
# migrate_db.sh — idempotent schema migrations for databases predating a feature.
#
# freeholdy has no migrations framework: init_db() (app/main.py lifespan) creates the full
# schema for a FRESH database, but an EXISTING database can be missing columns/tables added
# by a later feature. This script upgrades such a DB in place — no data loss. It self-checks
# and is safe to run repeatedly. It covers:
#   1. blue/green versioning — projects.backup_limit / projects.version_counter columns and
#      the project_versions table; backfills each existing dockerfile project as active v1.
#   2. unexposed compose services — makes compose_services.subdomain/local_port/container_port
#      nullable and adds a compose_services.exposed column (all existing rows are exposed).
#      Row backfill for previously-untracked unexposed services happens at app startup
#      (needs YAML parsing) — see app/services/compose_service.backfill_unexposed_services.
#
# Usage:
#   ./migrate_db.sh [path/to/freeholdy.db]
# Defaults to "${DATA_DIR:-data}/freeholdy.db".

set -euo pipefail

DB="${1:-${DATA_DIR:-data}/freeholdy.db}"

if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "error: sqlite3 CLI not found — install it (e.g. 'apt-get install sqlite3') and retry" >&2
    exit 1
fi

if [ ! -f "$DB" ]; then
    echo "No database at '$DB' — nothing to migrate (a fresh DB gets the full schema on startup)."
    exit 0
fi

# A pre-schema DB (no projects table yet) also needs nothing — init_db() will create it.
has_projects="$(sqlite3 "$DB" "SELECT name FROM sqlite_master WHERE type='table' AND name='projects';")"
if [ -z "$has_projects" ]; then
    echo "No 'projects' table in '$DB' — nothing to migrate."
    exit 0
fi

has_backup_limit="$(sqlite3 "$DB" "SELECT 1 FROM pragma_table_info('projects') WHERE name='backup_limit';")"
has_version_counter="$(sqlite3 "$DB" "SELECT 1 FROM pragma_table_info('projects') WHERE name='version_counter';")"
has_versions="$(sqlite3 "$DB" "SELECT name FROM sqlite_master WHERE type='table' AND name='project_versions';")"

# compose_services.exposed marks the unexposed-services migration as applied. The table
# exists whenever projects does (both are part of the base schema).
has_compose_services="$(sqlite3 "$DB" "SELECT name FROM sqlite_master WHERE type='table' AND name='compose_services';")"
has_exposed=""
if [ -n "$has_compose_services" ]; then
    has_exposed="$(sqlite3 "$DB" "SELECT 1 FROM pragma_table_info('compose_services') WHERE name='exposed';")"
fi

bluegreen_done=""
if [ -n "$has_backup_limit" ] && [ -n "$has_version_counter" ] && [ -n "$has_versions" ]; then
    bluegreen_done=1
fi
compose_done=""
if [ -z "$has_compose_services" ] || [ -n "$has_exposed" ]; then
    compose_done=1
fi

if [ -n "$bluegreen_done" ] && [ -n "$compose_done" ]; then
    echo "Database '$DB' is already up to date — nothing to migrate."
    exit 0
fi

echo "Migrating '$DB'…"

# Build only the statements that are actually missing (each guarded), then run them in one
# transaction so a partial migration can't leave the DB half-upgraded.
SQL="BEGIN;"

if [ -z "$has_backup_limit" ]; then
    echo "  + projects.backup_limit"
    SQL="$SQL ALTER TABLE projects ADD COLUMN backup_limit INTEGER NOT NULL DEFAULT 5;"
fi
if [ -z "$has_version_counter" ]; then
    echo "  + projects.version_counter"
    SQL="$SQL ALTER TABLE projects ADD COLUMN version_counter INTEGER NOT NULL DEFAULT 0;"
fi
if [ -z "$has_versions" ]; then
    echo "  + project_versions table"
    SQL="$SQL
    CREATE TABLE project_versions (
        id             INTEGER PRIMARY KEY,
        project_id     INTEGER NOT NULL REFERENCES projects(id),
        version        INTEGER NOT NULL,
        image_name     VARCHAR NOT NULL,
        container_name VARCHAR NOT NULL,
        local_port     INTEGER,
        status         VARCHAR NOT NULL,
        created_at     DATETIME
    );"
fi

if [ -z "$bluegreen_done" ]; then
    # Backfill: every existing dockerfile project with a container becomes its active version 1.
    # Guarded by NOT IN (…) so re-runs never duplicate rows.
    echo "  + backfilling existing dockerfile projects as active v1"
    SQL="$SQL
    INSERT INTO project_versions (project_id, version, image_name, container_name, local_port, status, created_at)
    SELECT id, 1, image_name, container_name, local_port, 'active', CURRENT_TIMESTAMP
      FROM projects
     WHERE deploy_mode = 'dockerfile'
       AND container_name IS NOT NULL
       AND image_name IS NOT NULL
       AND id NOT IN (SELECT project_id FROM project_versions);
    UPDATE projects
       SET version_counter = 1
     WHERE deploy_mode = 'dockerfile'
       AND container_name IS NOT NULL
       AND version_counter = 0
       AND id IN (SELECT project_id FROM project_versions);"
fi

# compose_services: drop the NOT NULL on subdomain/local_port/container_port and add the
# `exposed` column. SQLite can't ALTER these in place, so rebuild the table and copy rows
# (all existing rows are exposed by definition). No other table references compose_services.
if [ -z "$compose_done" ]; then
    echo "  + rebuilding compose_services (nullable subdomain/port + exposed column)"
    SQL="$SQL
    CREATE TABLE compose_services_new (
        id             INTEGER PRIMARY KEY,
        project_id     INTEGER NOT NULL REFERENCES projects(id),
        name           VARCHAR NOT NULL,
        exposed        BOOLEAN NOT NULL DEFAULT 1,
        subdomain      VARCHAR,
        custom_domain  VARCHAR,
        local_port     INTEGER UNIQUE,
        container_port INTEGER,
        container_name VARCHAR NOT NULL,
        ssl_enabled    BOOLEAN,
        websocket      BOOLEAN
    );
    INSERT INTO compose_services_new
        (id, project_id, name, exposed, subdomain, custom_domain, local_port, container_port, container_name, ssl_enabled, websocket)
    SELECT id, project_id, name, 1, subdomain, custom_domain, local_port, container_port, container_name, ssl_enabled, websocket
      FROM compose_services;
    DROP TABLE compose_services;
    ALTER TABLE compose_services_new RENAME TO compose_services;"
fi

SQL="$SQL COMMIT;"

sqlite3 "$DB" "$SQL"

backfilled="$(sqlite3 "$DB" "SELECT COUNT(*) FROM project_versions;")"
echo "Done. project_versions now holds $backfilled version row(s)."
