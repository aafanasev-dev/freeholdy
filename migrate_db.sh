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
#   3. compose versioning — makes project_versions.image_name/container_name nullable
#      (compose versions leave them NULL; per-service tags are deterministic). Backfill of
#      existing compose projects as active v1 happens at app startup (needs docker + YAML) —
#      see app/services/deploy_service.backfill_compose_versions.
#   4. environment variables — adds the project_env_files table holding one .env-format file
#      per project (service_name = '') and per compose service. Nothing to backfill: a
#      project with no row simply has no env, which is the pre-feature behaviour.
#   5. token roles — adds tokens.role ('admin' | 'guest') and the token_projects table (the
#      projects a guest token may act on; empty for admin). No backfill: the role column's
#      DEFAULT makes every pre-existing token an admin, i.e. exactly what it was before roles
#      existed. An intermediate dev build stored a single binding in tokens.project_id; if
#      that column is present its rows are copied into token_projects. The column itself is
#      left alone — nothing reads it, and dropping one in SQLite means rebuilding the table.
#   6. git origin — adds projects.git_url / projects.git_branch, recorded by a git deploy and
#      cleared by a file upload, so POST /projects/{name}/redeploy can re-clone from the
#      stored origin. Nothing to backfill: an existing project has no recorded origin until
#      its next git deploy (the .git dir on disk is not read).
#   7. backups — adds the backups table (one row per archive; project_id NULL = an archive of
#      the freeholdy database itself) and the backup_configs table (automatic-backup settings
#      per scope: cron schedule, on-deploy trigger, retention, destination name). Nothing to
#      backfill: a scope with no config row has automatic backups off, which is exactly the
#      pre-feature behaviour, and backup_service.get_config creates the row on first access.
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

# project_versions.image_name NOT NULL marks a pre-compose-versioning table (compose
# versions need it nullable). A missing table is created nullable by the bluegreen step.
has_env_files="$(sqlite3 "$DB" "SELECT name FROM sqlite_master WHERE type='table' AND name='project_env_files';")"

# tokens.role / tokens.project_id mark the token-roles migration as applied. The tokens
# table may legitimately not exist yet (a DB whose server never minted one) — treat that
# as done; init_db() creates it with both columns.
has_tokens="$(sqlite3 "$DB" "SELECT name FROM sqlite_master WHERE type='table' AND name='tokens';")"
has_token_role=""
has_token_project_col=""
if [ -n "$has_tokens" ]; then
    has_token_role="$(sqlite3 "$DB" "SELECT 1 FROM pragma_table_info('tokens') WHERE name='role';")"
    # Vestigial single-binding column from an intermediate dev build — its rows are migrated
    # into token_projects below, then it is left in place (nothing reads it).
    has_token_project_col="$(sqlite3 "$DB" "SELECT 1 FROM pragma_table_info('tokens') WHERE name='project_id';")"
fi
has_token_projects="$(sqlite3 "$DB" "SELECT name FROM sqlite_master WHERE type='table' AND name='token_projects';")"
roles_done=""
if [ -z "$has_tokens" ] || { [ -n "$has_token_role" ] && [ -n "$has_token_projects" ]; }; then
    roles_done=1
fi

has_git_url="$(sqlite3 "$DB" "SELECT 1 FROM pragma_table_info('projects') WHERE name='git_url';")"
has_git_branch="$(sqlite3 "$DB" "SELECT 1 FROM pragma_table_info('projects') WHERE name='git_branch';")"
git_origin_done=""
if [ -n "$has_git_url" ] && [ -n "$has_git_branch" ]; then
    git_origin_done=1
fi

# The two backup tables. Both are created whole (no column-level upgrades yet), so their
# presence alone marks migration 7 as applied.
has_backups="$(sqlite3 "$DB" "SELECT name FROM sqlite_master WHERE type='table' AND name='backups';")"
has_backup_configs="$(sqlite3 "$DB" "SELECT name FROM sqlite_master WHERE type='table' AND name='backup_configs';")"
backups_done=""
if [ -n "$has_backups" ] && [ -n "$has_backup_configs" ]; then
    backups_done=1
fi

versions_notnull=""
if [ -n "$has_versions" ]; then
    versions_notnull="$(sqlite3 "$DB" "SELECT 1 FROM pragma_table_info('project_versions') WHERE name='image_name' AND \"notnull\"=1;")"
fi

if [ -n "$bluegreen_done" ] && [ -n "$compose_done" ] && [ -z "$versions_notnull" ] \
   && [ -n "$has_env_files" ] && [ -n "$roles_done" ] && [ -n "$git_origin_done" ] \
   && [ -n "$backups_done" ]; then
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
        image_name     VARCHAR,
        container_name VARCHAR,
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

# project_versions: drop the NOT NULL on image_name/container_name (compose versions leave
# them NULL). SQLite can't ALTER these in place, so rebuild the table and copy rows.
if [ -n "$versions_notnull" ]; then
    echo "  + rebuilding project_versions (nullable image_name/container_name)"
    SQL="$SQL
    CREATE TABLE project_versions_new (
        id             INTEGER PRIMARY KEY,
        project_id     INTEGER NOT NULL REFERENCES projects(id),
        version        INTEGER NOT NULL,
        image_name     VARCHAR,
        container_name VARCHAR,
        local_port     INTEGER,
        status         VARCHAR NOT NULL,
        created_at     DATETIME
    );
    INSERT INTO project_versions_new
        (id, project_id, version, image_name, container_name, local_port, status, created_at)
    SELECT id, project_id, version, image_name, container_name, local_port, status, created_at
      FROM project_versions;
    DROP TABLE project_versions;
    ALTER TABLE project_versions_new RENAME TO project_versions;"
fi

# project_env_files: one .env-format file per project (service_name = '') and per compose
# service. Plain CREATE TABLE — no backfill, a project without a row just has no env.
if [ -z "$has_env_files" ]; then
    echo "  + project_env_files table"
    SQL="$SQL
    CREATE TABLE project_env_files (
        id           INTEGER PRIMARY KEY,
        project_id   INTEGER NOT NULL REFERENCES projects(id),
        service_name VARCHAR NOT NULL DEFAULT '',
        content      TEXT NOT NULL DEFAULT '',
        updated_at   DATETIME
    );
    CREATE UNIQUE INDEX ux_project_env_files ON project_env_files (project_id, service_name);"
fi

# tokens.role: a guarded ALTER — the DEFAULT backfills every existing token as an admin,
# which is what it effectively was before roles existed.
if [ -n "$has_tokens" ] && [ -z "$has_token_role" ]; then
    echo "  + tokens.role"
    SQL="$SQL ALTER TABLE tokens ADD COLUMN role VARCHAR NOT NULL DEFAULT 'admin';"
fi
# token_projects: which projects each guest token may act on (many-to-many).
if [ -z "$has_token_projects" ]; then
    echo "  + token_projects table"
    SQL="$SQL
    CREATE TABLE token_projects (
        token_id   INTEGER NOT NULL REFERENCES tokens(id),
        project_id INTEGER NOT NULL REFERENCES projects(id),
        PRIMARY KEY (token_id, project_id)
    );"
    # Carry over bindings from the intermediate single-project column, if this DB has it.
    if [ -n "$has_token_project_col" ]; then
        echo "  + migrating tokens.project_id bindings into token_projects"
        SQL="$SQL
        INSERT OR IGNORE INTO token_projects (token_id, project_id)
        SELECT id, project_id FROM tokens WHERE project_id IS NOT NULL;"
    fi
fi

# projects.git_url / git_branch: the origin a git deploy came from, so redeploy can re-clone
# it without being handed the URL again. NULL means "not git-backed".
if [ -z "$has_git_url" ]; then
    echo "  + projects.git_url"
    SQL="$SQL ALTER TABLE projects ADD COLUMN git_url VARCHAR;"
fi
if [ -z "$has_git_branch" ]; then
    echo "  + projects.git_branch"
    SQL="$SQL ALTER TABLE projects ADD COLUMN git_branch VARCHAR;"
fi

if [ -z "$has_backups" ]; then
    echo "  + backups table"
    SQL="$SQL
    CREATE TABLE backups (
        id            INTEGER PRIMARY KEY,
        project_id    INTEGER REFERENCES projects(id),
        version       INTEGER,
        kind          VARCHAR NOT NULL DEFAULT 'manual',
        imported      BOOLEAN NOT NULL DEFAULT 0,
        filename      VARCHAR NOT NULL,
        size_bytes    INTEGER,
        sha256        VARCHAR,
        has_images    BOOLEAN NOT NULL DEFAULT 0,
        has_volumes   BOOLEAN NOT NULL DEFAULT 0,
        has_env       BOOLEAN NOT NULL DEFAULT 0,
        has_project   BOOLEAN NOT NULL DEFAULT 0,
        status        VARCHAR NOT NULL DEFAULT 'creating',
        message       TEXT NOT NULL DEFAULT '',
        target_name   VARCHAR,
        remote_status VARCHAR NOT NULL DEFAULT 'none',
        remote_path   VARCHAR,
        created_at    DATETIME
    );
    CREATE INDEX ix_backups_project ON backups (project_id);"
fi

if [ -z "$has_backup_configs" ]; then
    echo "  + backup_configs table"
    SQL="$SQL
    CREATE TABLE backup_configs (
        id              INTEGER PRIMARY KEY,
        project_id      INTEGER REFERENCES projects(id),
        enabled         BOOLEAN NOT NULL DEFAULT 0,
        schedule_cron   VARCHAR,
        on_deploy       BOOLEAN NOT NULL DEFAULT 0,
        keep_local      INTEGER NOT NULL DEFAULT 5,
        keep_remote     INTEGER NOT NULL DEFAULT 0,
        target_name     VARCHAR,
        include_volumes BOOLEAN NOT NULL DEFAULT 1,
        last_run_at     DATETIME,
        last_status     VARCHAR,
        last_message    TEXT NOT NULL DEFAULT '',
        created_at      DATETIME,
        updated_at      DATETIME
    );
    CREATE UNIQUE INDEX ux_backup_configs ON backup_configs (project_id);"
fi

SQL="$SQL COMMIT;"

sqlite3 "$DB" "$SQL"

backfilled="$(sqlite3 "$DB" "SELECT COUNT(*) FROM project_versions;")"
echo "Done. project_versions now holds $backfilled version row(s)."
