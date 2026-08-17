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

import sqlite3
from pathlib import Path

from app import config

# ISO 8601, UTC, second precision. Text in this shape sorts chronologically, so
# ORDER BY on a timestamp column is correct without any conversion. Every
# timestamp default in migrations/ uses this same expression.
SQLITE_NOW = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"


class Fts5Unavailable(RuntimeError):
    """SQLite was built without the FTS5 full-text search extension."""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open the database, creating its parent directory if needed."""
    path = Path(db_path) if db_path is not None else config.DB_PATH
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path), isolation_level=None, timeout=10.0)
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
