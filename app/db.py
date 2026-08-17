"""SQLite connection handling.

SPEC §4 requires WAL mode and requires that FTS5 be *verified* at startup with a
loud failure if it is missing. ``require_fts5`` does that by actually creating a
temporary FTS5 table, which is more trustworthy than reading
``PRAGMA compile_options`` and hoping the string means what it looks like.

All connections here run in autocommit mode (``isolation_level=None``). Python's
sqlite3 module otherwise opens transactions implicitly at times that are hard to
predict, which collides with the explicit transaction control that
``app.migrate`` needs.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from app import config

# ISO 8601, UTC, second precision. Text in this shape sorts chronologically, so
# ORDER BY on a timestamp column is correct without any conversion. Every
# timestamp default in migrations/ uses this same expression.
SQLITE_NOW = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"


class Fts5Unavailable(RuntimeError):
    """SQLite was built without the FTS5 full-text search extension."""


class DatabaseUnavailable(RuntimeError):
    """The database file could not be opened, most often a permissions problem."""


def _unavailable(path: Path, cause: Exception) -> DatabaseUnavailable:
    """Turn SQLite's opaque open failure into something actionable.

    SQLite reports every open failure as "unable to open database file",
    regardless of whether the directory is missing, unwritable, or owned by
    somebody else. Inside a container that message names a path the reader cannot
    see from the host, so it needs the host-side fix spelled out. SPEC §4 asks
    for loud failures; a loud failure that does not say what to do is only half
    of that.
    """
    directory = path.parent
    details = [
        f"Could not open the database at {path}.",
        f"SQLite reported: {cause}",
        "",
    ]

    if not directory.is_dir():
        details += [
            f"The directory {directory} does not exist.",
            "Inside the container this path comes from the volume line in "
            "docker-compose.yml, so on the server create it with:",
            "",
            "    sudo mkdir -p /srv/dashboard/data",
            "    sudo chown -R 1000:1000 /srv/dashboard/data",
        ]
    else:
        details += [
            f"The directory {directory} exists but this process cannot write to "
            f"it. This process is running as user id {os.getuid()}, and the "
            f"directory has to be writable by that id. In the container the "
            f"dashboard always runs as id 1000.",
            "",
            "On the server, check what owns it:",
            "",
            "    ls -ld /srv/dashboard/data",
            "",
            "and hand it over if the owner is root:",
            "",
            "    sudo chown -R 1000:1000 /srv/dashboard/data",
            "    sudo docker compose up -d",
        ]

    details += ["", "See docs/OPERATIONS.md for more."]
    return DatabaseUnavailable("\n".join(details))


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open the database, creating its parent directory if needed."""
    path = Path(db_path) if db_path is not None else config.DB_PATH
    if str(path) != ":memory:":
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise _unavailable(path, exc) from exc

    try:
        conn = sqlite3.connect(str(path), isolation_level=None, timeout=10.0)
    except sqlite3.OperationalError as exc:
        raise _unavailable(path, exc) from exc

    conn.row_factory = sqlite3.Row
    apply_pragmas(conn)
    return conn


def apply_pragmas(conn: sqlite3.Connection) -> None:
    """Configure the connection and, once, the database file itself."""
    # Persistent, stored in the database file: survives restarts, harmless to
    # repeat. WAL lets the web app read while a scheduled job writes, and is what
    # makes the nightly `sqlite3 .backup` safe to run against a live database.
    conn.execute("PRAGMA journal_mode = WAL")

    # Per-connection, so these must be set every time.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    # NORMAL is the standard companion to WAL: a crash can lose the most recent
    # transactions but cannot corrupt the database.
    conn.execute("PRAGMA synchronous = NORMAL")


def journal_mode(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA journal_mode").fetchone()
    return str(row[0]).lower()


def fts5_available(conn: sqlite3.Connection) -> bool:
    """Return True if this SQLite build can create FTS5 tables."""
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.fts5_probe USING fts5(x)")
    except sqlite3.OperationalError:
        return False
    conn.execute("DROP TABLE temp.fts5_probe")
    return True


def require_fts5(conn: sqlite3.Connection) -> None:
    """Raise Fts5Unavailable if FTS5 is missing.

    FTS5 is the entire search story for the document archive (SPEC §7 — no
    vectors, no second vendor). A build without it would appear to work until M4
    and then fail with an obscure error, so refuse to start instead.
    """
    if not fts5_available(conn):
        raise Fts5Unavailable(
            "This SQLite build has no FTS5 full-text search support, which the "
            "document archive depends on. The official python:3.12-slim-bookworm "
            "image does include it, so this usually means the base image in the "
            "Dockerfile was changed. See docs/OPERATIONS.md."
        )
