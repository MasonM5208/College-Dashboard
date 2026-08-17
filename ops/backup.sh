#!/usr/bin/env bash
#
# Nightly encrypted backup of the dashboard database (SPEC §11).
#
#   1. Take a proper online copy with sqlite3's .backup command.
#   2. Check that copy is intact before trusting it.
#   3. Encrypt it with age, to a public key. The private key is not on this server.
#   4. Copy it off the server to Backblaze B2.
#   5. Prune old copies, locally and remotely.
#   6. Record the outcome in the database, so the dashboard shows it.
#
# Run from root's crontab — see ops/crontab.example. It has to be root, because the
# secrets it needs are in a root-only file. Everything that touches the database
# itself is dropped back to the database's owner first, so this script never leaves
# root-owned files where the container expects to write.
#
# Restoring is a separate script: ops/restore.sh.

set -euo pipefail

# Never turn on tracing in here. `set -x` would print the contents of the secrets
# file to the log.

# --- settings ---------------------------------------------------------------

ENV_FILE="${ENV_FILE:-/etc/college-dashboard/env}"
DB="${DB_PATH:-/srv/dashboard/data/dashboard.db}"
BACKUP_DIR="${BACKUP_DIR:-/srv/dashboard/backups}"

# Retention, per SPEC §11: "Retain 30 dailies and 6 monthlies."
KEEP_DAILY="${BACKUP_KEEP_DAILY:-30}"
KEEP_MONTHLY="${BACKUP_KEEP_MONTHLY:-6}"

# Load the secrets file, unless the values are already in the environment. `set -a`
# exports everything the file defines so age and rclone can see it.
if [ -r "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE_NAME="dashboard-${TIMESTAMP}.db.age"
STAGE=""
STAGE_NAME="starting up"

log() { printf '%s [backup] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

# --- run database commands as the database's owner ---------------------------
#
# Reading a database in WAL mode creates -wal and -shm files beside it. If root
# created those, the container (running as uid 1000) could no longer write to its
# own database. So every sqlite3 call goes through here.

db_owner() {
  stat -c %U "$DB" 2>/dev/null || stat -f %Su "$DB"
}

as_db_owner() {
  local owner
  owner="$(db_owner)"
  if [ "$(id -un)" = "$owner" ]; then
    "$@"
  elif command -v runuser >/dev/null 2>&1; then
    runuser -u "$owner" -- "$@"
  else
    die "cannot drop privileges to ${owner}: runuser is not installed"
  fi
}

# --- record the outcome in sync_state ---------------------------------------
#
# SPEC §4: "Every scheduled job writes to sync_state and logs its outcome." The
# dashboard reads this table, so a backup that stops running becomes visible on the
# status page instead of being discovered when a restore is needed.
#
# Only the name of the failing stage is stored, never the underlying command's
# output: SPEC §11 forbids secrets in error messages, and rclone's errors can name
# account identifiers. The full detail goes to this script's log, which is
# root-only.

record_sync_state() {
  local error="$1"
  local now
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  if [ -z "$error" ]; then
    as_db_owner sqlite3 "$DB" "
      INSERT INTO sync_state (source, last_attempt_at, last_success_at,
                              last_error, consecutive_failures)
      VALUES ('backup', '$now', '$now', NULL, 0)
      ON CONFLICT(source) DO UPDATE SET
        last_attempt_at      = '$now',
        last_success_at      = '$now',
        last_error           = NULL,
        consecutive_failures = 0;" || log "warning: could not update sync_state"
  else
    local escaped=${error//\'/\'\'}
    as_db_owner sqlite3 "$DB" "
      INSERT INTO sync_state (source, last_attempt_at, last_success_at,
                              last_error, consecutive_failures)
      VALUES ('backup', '$now', NULL, '$escaped', 1)
      ON CONFLICT(source) DO UPDATE SET
        last_attempt_at      = '$now',
        last_error           = '$escaped',
        consecutive_failures = sync_state.consecutive_failures + 1;" \
      || log "warning: could not update sync_state"
  fi
}

cleanup() {
  if [ -n "$STAGE" ] && [ -d "$STAGE" ]; then
    rm -rf "$STAGE"
  fi
  # Always succeed. This runs from the EXIT trap, and bash would otherwise report
  # the trap's own status as the script's exit code, hiding the real failure.
  return 0
}

on_error() {
  local code=$?
  log "FAILED during: ${STAGE_NAME} (exit ${code})"
  record_sync_state "backup failed during: ${STAGE_NAME}"
  cleanup
  exit "$code"
}

trap on_error ERR
trap cleanup EXIT

# --- checks -----------------------------------------------------------------

STAGE_NAME="checking prerequisites"

[ -f "$DB" ] || die "no database at ${DB}"

for tool in sqlite3 age rclone; do
  command -v "$tool" >/dev/null 2>&1 \
    || die "${tool} is not installed. Install it with: sudo apt install sqlite3 age rclone"
done

: "${BACKUP_AGE_RECIPIENT:?BACKUP_AGE_RECIPIENT is not set. It is the age public key backups are encrypted to — see docs/SECRETS.md}"
: "${BACKUP_B2_BUCKET:?BACKUP_B2_BUCKET is not set — see docs/SECRETS.md}"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

# The staging directory has to be writable by the database's owner, because that
# is who runs the .backup command.
STAGE="$(mktemp -d "${BACKUP_DIR}/.staging.XXXXXX")"
chown "$(db_owner)" "$STAGE"
chmod 700 "$STAGE"

# --- 1. online copy ---------------------------------------------------------
#
# .backup is SQLite's own backup API, not a file copy. SPEC §11 is explicit about
# this: in WAL mode the database is spread across dashboard.db and dashboard.db-wal,
# so `cp` can capture a torn state that will not open. .backup is safe to run while
# the dashboard is serving requests.

STAGE_NAME="copying the database"
log "copying ${DB}"
as_db_owner sqlite3 "$DB" ".backup '${STAGE}/dashboard.db'"

# --- 2. verify the copy before trusting it ----------------------------------

STAGE_NAME="verifying the copy"
CHECK="$(as_db_owner sqlite3 "${STAGE}/dashboard.db" 'PRAGMA integrity_check;')"
[ "$CHECK" = "ok" ] || die "the copy is damaged; integrity_check said: ${CHECK}"

ROWS="$(as_db_owner sqlite3 "${STAGE}/dashboard.db" \
  "SELECT COUNT(*) FROM sqlite_master WHERE type='table';")"
log "copy verified: intact, ${ROWS} tables"

# --- 3. encrypt -------------------------------------------------------------
#
# Encrypted to a public key. The matching private key is deliberately absent from
# this server: SPEC §11 treats the VPS as something that can vanish or be broken
# into, and backups nobody on this machine can read stay useful in both cases.

STAGE_NAME="encrypting"
age -r "$BACKUP_AGE_RECIPIENT" -o "${STAGE}/${ARCHIVE_NAME}" "${STAGE}/dashboard.db"
rm -f "${STAGE}/dashboard.db"

install -m 600 "${STAGE}/${ARCHIVE_NAME}" "${BACKUP_DIR}/${ARCHIVE_NAME}"
log "encrypted to ${ARCHIVE_NAME} ($(wc -c < "${BACKUP_DIR}/${ARCHIVE_NAME}" | tr -d ' ') bytes)"

# --- 4. off-site ------------------------------------------------------------

STAGE_NAME="copying off-site to Backblaze B2"
rclone copyto --quiet \
  "${BACKUP_DIR}/${ARCHIVE_NAME}" \
  "b2:${BACKUP_B2_BUCKET}/dashboard/${ARCHIVE_NAME}"
log "copied off-site to b2:${BACKUP_B2_BUCKET}/dashboard/${ARCHIVE_NAME}"

# --- 5. prune ---------------------------------------------------------------
#
# Keeps the newest KEEP_DAILY archives, plus the newest archive from each of the
# KEEP_MONTHLY most recent months. Filenames sort chronologically, which is the
# whole reason for the YYYYMMDDTHHMMSSZ stamp.

# Reads archive names on stdin, newest first, and writes back the ones to keep.
names_to_keep() {
  awk -v keep_daily="$KEEP_DAILY" -v keep_monthly="$KEEP_MONTHLY" '
    NF == 0 { next }
    { names[++n] = $0 }
    END {
      # The newest keep_daily archives, whatever their dates.
      for (i = 1; i <= n && i <= keep_daily; i++) keep[names[i]] = 1

      # Then the newest surviving archive from each of the keep_monthly most
      # recent months. "dashboard-" is ten characters, so the YYYYMM starts at 11.
      months = 0
      for (i = 1; i <= n; i++) {
        month = substr(names[i], 11, 6)
        if (month in seen) continue
        seen[month] = 1
        if (++months <= keep_monthly) keep[names[i]] = 1
      }

      for (i = 1; i <= n; i++) if (names[i] in keep) print names[i]
    }
  '
}

# Deletes everything names_to_keep did not select. `lister` prints the current
# archive names; `deleter` removes one by name.
prune() {
  local label="$1" lister="$2" deleter="$3"
  local all keep

  all="$($lister | sort -r || true)"
  [ -z "$all" ] && return 0
  keep="$(printf '%s\n' "$all" | names_to_keep)"

  printf '%s\n' "$all" | while IFS= read -r name; do
    [ -z "$name" ] && continue
    if ! printf '%s\n' "$keep" | grep -qxF "$name"; then
      "$deleter" "$name"
      log "pruned ${label} ${name}"
    fi
  done
}

list_local()   { cd "$BACKUP_DIR" && ls -1 dashboard-*.db.age 2>/dev/null; }
delete_local() { rm -f "${BACKUP_DIR}/$1"; }

list_remote() {
  rclone lsf "b2:${BACKUP_B2_BUCKET}/dashboard/" 2>/dev/null \
    | grep -E '^dashboard-.*\.db\.age$' || true
}
delete_remote() {
  rclone deletefile --quiet "b2:${BACKUP_B2_BUCKET}/dashboard/$1"
}

STAGE_NAME="pruning old local backups"
prune "local" list_local delete_local

STAGE_NAME="pruning old off-site backups"
prune "off-site" list_remote delete_remote

# --- 6. record success ------------------------------------------------------

STAGE_NAME="recording the outcome"
record_sync_state ""

log "done: ${ARCHIVE_NAME}"
