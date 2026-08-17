#!/usr/bin/env bash
#
# Restore an encrypted backup to a scratch file and check it.
#
# SPEC §11: "An untested backup is not a backup." This script is what makes the
# test cheap enough to actually do, and docs/OPERATIONS.md walks through it.
#
# Normally run on the MacBook, not on the server. The private key that decrypts
# backups is deliberately not on the server, and the situation worth rehearsing is
# the one where the server is gone.
#
# Usage:
#   ops/restore.sh <encrypted-backup> <destination.db> [--identity <key-file>]
#
# Example:
#   ops/restore.sh ~/Downloads/dashboard-20260817T031500Z.db.age /tmp/check.db
#
# It will not overwrite an existing file, and it refuses to write over a database
# that is currently in use. Putting a restored copy back into service is a separate,
# deliberate act described in docs/OPERATIONS.md.

set -euo pipefail

IDENTITY="${BACKUP_AGE_IDENTITY:-$HOME/.age/dashboard-backup.key}"
ARCHIVE=""
DESTINATION=""

usage() {
  cat >&2 <<'USAGE'
Usage: ops/restore.sh <encrypted-backup> <destination.db> [--identity <key-file>]

  <encrypted-backup>  a file named like dashboard-20260817T031500Z.db.age
  <destination.db>    where to write the restored database; must not exist yet
  --identity          the age private key file
                      (default: ~/.age/dashboard-backup.key, or $BACKUP_AGE_IDENTITY)
USAGE
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --identity) IDENTITY="${2:-}"; shift 2 ;;
    -h|--help)  usage ;;
    -*)         echo "Unknown option: $1" >&2; usage ;;
    *)
      if   [ -z "$ARCHIVE" ];     then ARCHIVE="$1"
      elif [ -z "$DESTINATION" ]; then DESTINATION="$1"
      else echo "Too many arguments." >&2; usage
      fi
      shift ;;
  esac
done

[ -n "$ARCHIVE" ] && [ -n "$DESTINATION" ] || usage

log() { printf '[restore] %s\n' "$*"; }
die() { printf '[restore] ERROR: %s\n' "$*" >&2; exit 1; }

# --- checks -----------------------------------------------------------------

command -v age >/dev/null 2>&1 || die \
  "age is not installed. On a Mac: brew install age. On Debian: sudo apt install age"
command -v sqlite3 >/dev/null 2>&1 || die \
  "sqlite3 is not installed. On Debian: sudo apt install sqlite3"

[ -f "$ARCHIVE" ]  || die "no such backup file: ${ARCHIVE}"
[ -f "$IDENTITY" ] || die "no age private key at ${IDENTITY}. Pass --identity <file>."
# Refuse to write over a live database. Restoring into one while the dashboard is
# running would corrupt both the file and the running server's view of it.
#
# Checked before the generic "already exists" test below, so that aiming at the
# live database gives the message that explains what to do instead.
LIVE_DB="${DB_PATH:-/srv/dashboard/data/dashboard.db}"
DESTINATION_DIR="$(cd "$(dirname "$DESTINATION")" 2>/dev/null && pwd || true)"
if [ -n "$DESTINATION_DIR" ] \
   && [ "${DESTINATION_DIR}/$(basename "$DESTINATION")" = "$LIVE_DB" ]; then
  die "that is the live database, which must not be written to underneath a running
       dashboard. Restore to a scratch path instead, check it, and then follow
       'Putting a restored backup back into service' in docs/OPERATIONS.md."
fi

[ -e "$DESTINATION" ] && die \
  "${DESTINATION} already exists. Choose a path that does not, so nothing is overwritten."

# --- decrypt ----------------------------------------------------------------

log "decrypting ${ARCHIVE}"
age --decrypt -i "$IDENTITY" -o "$DESTINATION" "$ARCHIVE"

# --- verify -----------------------------------------------------------------

log "checking the restored file"
CHECK="$(sqlite3 "$DESTINATION" 'PRAGMA integrity_check;')"
[ "$CHECK" = "ok" ] || die "the restored database is damaged; SQLite said: ${CHECK}"

SCHEMA_VERSION="$(sqlite3 "$DESTINATION" \
  'SELECT COALESCE(MAX(version), 0) FROM schema_migrations;' 2>/dev/null || echo 0)"

echo
log "Restored successfully to ${DESTINATION}"
log "  integrity check: ok"
log "  schema version:  $(printf '%04d' "$SCHEMA_VERSION")"
echo
echo "What it contains:"
sqlite3 "$DESTINATION" <<'SQL'
.mode list
.separator '  '
SELECT 'courses',            COUNT(*) FROM courses
UNION ALL SELECT 'assignments',        COUNT(*) FROM assignments
UNION ALL SELECT 'reminder_instances', COUNT(*) FROM reminder_instances
UNION ALL SELECT 'audit_log entries',  COUNT(*) FROM audit_log;
SQL
echo
echo "This copy is a plain, unencrypted database file. Delete it when you are done:"
echo "  rm ${DESTINATION}"
