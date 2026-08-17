"""Forward-only schema migrations.

SPEC §5: "Implement with real migrations from commit one. Never edit the schema by
hand on the server."

The design is a directory of numbered ``.sql`` files plus this runner. There is no
ORM and no migration framework, because the schema needs FTS5 virtual tables and
their synchronisation triggers (SPEC §7), which frameworks built around model
autogeneration cannot express. A directory of plain SQL keeps that DDL literal and
reviewable.

Guarantees this runner provides:

* **Each file applies inside one transaction**, together with its own bookkeeping
  row. A file either applies completely or not at all.
* **Applied files are checksummed.** If a migration is edited after it was
  applied, the next start refuses to run and names the file. That is what
  mechanically enforces "never edit the schema by hand" — the same check catches a
  database that was altered outside of a migration.
* **Forward-only.** There are no down-migrations. A mistake is corrected by adding
  a new numbered file. Rationale in docs/ARCHITECTURE.md.

Run it directly to migrate without starting the web server:

    python -m app.migrate
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from app import config, db

log = logging.getLogger("migrate")

# 0001_core.sql — four digits, an underscore, a lowercase name.
FILENAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

# Statements a migration file must not contain, because this runner supplies the
# transaction itself. Matched only at the very start of a line, so the indented
# BEGIN ... END that delimits a trigger body is not affected.
FORBIDDEN_RE = re.compile(
    r"^(COMMIT|ROLLBACK|VACUUM|END\s+TRANSACTION)\b",
    re.IGNORECASE | re.MULTILINE,
)

TRACKING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    INTEGER PRIMARY KEY,
  filename   TEXT    NOT NULL,
  sha256     TEXT    NOT NULL,
  applied_at TEXT    NOT NULL
)
"""


class MigrationError(RuntimeError):
    """A migration could not be applied, or the migration history is inconsistent."""


@dataclass(frozen=True)
class Migration:
    version: int
    filename: str
    path: Path
    sha256: str
    sql: str


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def discover(directory: Path | None = None) -> list[Migration]:
    """Read and validate every migration file, ordered by version."""
    directory = directory or config.MIGRATIONS_DIR
    if not directory.is_dir():
        raise MigrationError(f"Migrations directory not found: {directory}")

    found: dict[int, Migration] = {}
    for path in sorted(directory.iterdir()):
        if path.name.startswith(".") or not path.is_file():
            continue
        if path.suffix != ".sql":
            continue

        match = FILENAME_RE.match(path.name)
        if not match:
            raise MigrationError(
                f"Migration filename {path.name!r} is not in the required form "
                f"NNNN_lowercase_name.sql (for example 0002_documents.sql)."
            )

        raw = path.read_bytes()
        text = raw.decode("utf-8")
        if not text.strip():
            raise MigrationError(f"Migration {path.name} is empty.")

        # Comments are stripped first so that prose mentioning one of these words
        # does not trip the check.
        without_comments = re.sub(r"--[^\n]*", "", text)
        if bad := FORBIDDEN_RE.search(without_comments):
            raise MigrationError(
                f"Migration {path.name} contains a top-level "
                f"{bad.group(1).upper()} statement. Migration files must not "
                f"manage transactions; app.migrate wraps each file in one."
            )

        version = int(match.group(1))
        if version in found:
            raise MigrationError(
                f"Two migrations share version {version:04d}: "
                f"{found[version].filename} and {path.name}."
            )
        if version < 1:
            raise MigrationError(f"Migration versions start at 0001, got {path.name}.")

        found[version] = Migration(
            version=version,
            filename=path.name,
            path=path,
            sha256=_sha256(raw),
            sql=text,
        )

    return [found[v] for v in sorted(found)]


def ensure_tracking_table(conn: sqlite3.Connection) -> None:
    conn.execute(TRACKING_TABLE_SQL)


def applied_rows(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    rows = conn.execute(
        "SELECT version, filename, sha256, applied_at FROM schema_migrations"
    ).fetchall()
    return {int(row["version"]): row for row in rows}


def current_version(conn: sqlite3.Connection) -> int:
    """Highest applied migration version, or 0 on a database with none."""
    try:
        row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row["v"]) if row and row["v"] is not None else 0


def pending(
    conn: sqlite3.Connection, migrations_dir: Path | None = None
) -> list[Migration]:
    """Migrations that exist on disk but have not been applied to this database.

    Read-only: unlike ``run``, this creates nothing. A database with no tracking
    table at all reports every migration as pending.
    """
    try:
        applied = applied_rows(conn)
    except sqlite3.OperationalError:
        applied = {}
    return [m for m in discover(migrations_dir) if m.version not in applied]


def _check_history(migrations: list[Migration], applied: dict[int, sqlite3.Row]) -> None:
    """Refuse to continue if the files on disk disagree with what was applied."""
    by_version = {m.version: m for m in migrations}

    for version, row in sorted(applied.items()):
        migration = by_version.get(version)
        if migration is None:
            raise MigrationError(
                f"Migration {version:04d} ({row['filename']}) is recorded as applied "
                f"on {row['applied_at']}, but its file is missing from "
                f"{config.MIGRATIONS_DIR}. Restore the file — do not delete the "
                f"record. See docs/OPERATIONS.md."
            )
        if migration.sha256 != row["sha256"]:
            raise MigrationError(
                f"Migration {migration.filename} was modified after it was applied "
                f"on {row['applied_at']}. An applied migration must never be edited, "
                f"because the database has already been changed by the old version of "
                f"the file. Revert this file to its committed contents and put the new "
                f"change in a new numbered migration instead. See docs/OPERATIONS.md."
            )

    highest_applied = max(applied, default=0)
    for migration in migrations:
        if migration.version not in applied and migration.version < highest_applied:
            raise MigrationError(
                f"Migration {migration.filename} has not been applied, but the higher "
                f"version {highest_applied:04d} already has. Migrations apply in "
                f"order, so renumber this file above {highest_applied:04d}."
            )


def _apply_one(conn: sqlite3.Connection, migration: Migration) -> None:
    """Apply one migration and record it, atomically.

    ``executescript`` commits any transaction that is already open before it runs,
    so the BEGIN has to be part of the script itself rather than a separate
    statement. The script deliberately has no COMMIT: that leaves the transaction
    open so the bookkeeping row lands inside it.
    """
    cur = conn.cursor()
    try:
        cur.executescript("BEGIN;\n" + migration.sql)
        cur.execute(
            "INSERT INTO schema_migrations (version, filename, sha256, applied_at) "
            f"VALUES (?, ?, ?, {db.SQLITE_NOW})",
            (migration.version, migration.filename, migration.sha256),
        )
        conn.execute("COMMIT")
    except Exception as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise MigrationError(
            f"Migration {migration.filename} failed and was rolled back; the database "
            f"is unchanged by it. SQLite reported: {exc}"
        ) from exc


def run(
    db_path: Path | str | None = None,
    migrations_dir: Path | None = None,
) -> int:
    """Verify the database, apply pending migrations, return the schema version."""
    migrations = discover(migrations_dir)

    conn = db.connect(db_path)
    try:
        mode = db.journal_mode(conn)
        if mode != "wal":
            raise MigrationError(
                f"Expected the database to be in WAL mode but it reports {mode!r}. "
                f"WAL is what makes the nightly backup safe to take while the app is "
                f"running (SPEC §11)."
            )
        db.require_fts5(conn)

        ensure_tracking_table(conn)
        applied = applied_rows(conn)
        _check_history(migrations, applied)

        pending = [m for m in migrations if m.version not in applied]
        if not pending:
            version = current_version(conn)
            log.info("Schema is up to date at version %04d.", version)
            return version

        for migration in pending:
            log.info("Applying %s ...", migration.filename)
            _apply_one(conn, migration)
            log.info("Applied %s.", migration.filename)

        version = current_version(conn)
        log.info(
            "Applied %d migration(s). Schema version is now %04d.",
            len(pending),
            version,
        )
        return version
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [migrate] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    try:
        run()
    except (MigrationError, db.Fts5Unavailable, db.DatabaseUnavailable) as exc:
        # One blank line and a banner, because this is the message that appears in
        # `sudo docker compose logs` when the container will not start, and it has
        # to be findable by someone who is not looking for it.
        log.error("")
        log.error("=" * 72)
        log.error("DATABASE MIGRATION FAILED — the dashboard did not start.")
        log.error("=" * 72)
        for line in str(exc).splitlines():
            log.error("%s", line)
        log.error("=" * 72)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
