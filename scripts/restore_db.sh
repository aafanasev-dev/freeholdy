#!/usr/bin/env bash
#
# restore_db.sh — put a freeholdy database backup archive back in place.
#
# Restoring the database is deliberately NOT an API call: the server that would serve the
# request is the one holding the database open, and swapping the file under a running process
# is how you get a half-restored install. So this is a script that stops the service, swaps
# the file, and starts it again — with the database it replaces kept alongside.
#
# The archive is one produced by `POST /backups/database` (`fhcli db-backup`) and fetched with
# `fhcli db-backup-download`; it holds a gzipped SQLite file and a manifest.
#
# Usage:
#   sudo ./scripts/restore_db.sh path/to/_system-YYYYmmdd-HHMMSS.fhbak.tar [path/to/freeholdy.db]
# The database defaults to "${DATA_DIR:-data}/freeholdy.db" relative to the repo root.

set -euo pipefail

ARCHIVE="${1:-}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${2:-${REPO_ROOT}/${DATA_DIR:-data}/freeholdy.db}"
SERVICE="freeholdy"

if [ -z "$ARCHIVE" ] || [ ! -f "$ARCHIVE" ]; then
    echo "Usage: $0 <backup.fhbak.tar> [freeholdy.db]" >&2
    exit 1
fi
if ! tar tf "$ARCHIVE" >/dev/null 2>&1; then
    echo "'$ARCHIVE' is not a tar archive." >&2
    exit 1
fi
if ! tar tf "$ARCHIVE" | grep -q '^freeholdy\.db\.gz$'; then
    echo "'$ARCHIVE' holds no freeholdy.db.gz — it is a project backup, not a database one." >&2
    echo "Import a project backup with: fhcli backup-upload <project> '$ARCHIVE'" >&2
    exit 1
fi

echo "Archive : $ARCHIVE"
tar xOf "$ARCHIVE" manifest.json 2>/dev/null | sed 's/^/  /' || true
echo "Target  : $DB"
read -r -p "Replace that database? [y/N] " reply
[ "$reply" = "y" ] || [ "$reply" = "Y" ] || { echo "Aborted."; exit 0; }

STOPPED=""
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$SERVICE"; then
    echo "Stopping $SERVICE…"
    systemctl stop "$SERVICE"
    STOPPED=1
fi

STAGED="$(mktemp)"
trap 'rm -f "$STAGED"' EXIT
tar xOf "$ARCHIVE" freeholdy.db.gz | gunzip > "$STAGED"

# Refuse to install something that is not a readable database — better a failed restore than
# a service that starts against a corrupt file.
if command -v sqlite3 >/dev/null 2>&1; then
    if ! sqlite3 "$STAGED" "SELECT count(*) FROM sqlite_master;" >/dev/null 2>&1; then
        echo "The extracted file is not a readable SQLite database — nothing was changed." >&2
        [ -n "$STOPPED" ] && systemctl start "$SERVICE"
        exit 1
    fi
fi

if [ -f "$DB" ]; then
    KEEP="${DB}.replaced-$(date -u +%Y%m%d-%H%M%S)"
    cp -a "$DB" "$KEEP"
    echo "Previous database kept at $KEEP"
fi
mkdir -p "$(dirname "$DB")"
cp "$STAGED" "$DB"
# Match the owner of the directory it lives in, so the service user can still write it.
chown --reference="$(dirname "$DB")" "$DB" 2>/dev/null || true
echo "Restored $DB"

if [ -n "$STOPPED" ]; then
    echo "Starting $SERVICE…"
    systemctl start "$SERVICE"
fi
echo "Done. Run ./migrate_db.sh if the archive predates a schema change."
