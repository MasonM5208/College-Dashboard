"""The facts the status page and /healthz both report.

SPEC §4, "Fail loudly": *"A sync that silently stops is worse than one that
crashes — the owner will trust stale data. Surface last-successful-sync time
prominently in the UI."*

So this module exists in M0, before there is anything to sync, and the status page
shows the sync table from day one. When M1's Canvas polling arrives it writes a
`sync_state` row and appears here with no further work.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app import config, db, migrate

TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"

# How long a source may go without a success before the page calls it stale.
#
# canvas_ics: SPEC §6 — poll every 30 minutes, and warn past three hours.
# backup:     nightly, so a day plus a margin for a slow upload.
STALE_AFTER_HOURS = {
    "canvas_ics": 3.0,
    "caldav_push": 3.0,
    "backup": 30.0,
}
DEFAULT_STALE_AFTER_HOURS = 24.0

# A human label per source, so the page never shows a bare table name.
SOURCE_LABELS = {
    "canvas_ics": "Canvas calendar feed",
    "caldav_push": "Reminders sent to your iPhone",
    "backup": "Nightly backup",
}


def parse_timestamp(value: str | None) -> datetime | None:
    """Read one of our stored ISO 8601 UTC timestamps."""
    if not value:
        return None
    try:
        return datetime.strptime(value, TIMESTAMP_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _hours_since(value: str | None, now: datetime) -> float | None:
    when = parse_timestamp(value)
    if when is None:
        return None
    return (now - when).total_seconds() / 3600.0


def sync_sources(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    """One entry per scheduled job, with staleness already worked out."""
    now = now or datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT source, last_success_at, last_attempt_at, last_error, "
        "       consecutive_failures "
        "FROM sync_state ORDER BY source"
    ).fetchall()

    sources = []
    for row in rows:
        source = row["source"]
        age_hours = _hours_since(row["last_success_at"], now)
        limit = STALE_AFTER_HOURS.get(source, DEFAULT_STALE_AFTER_HOURS)
        failures = int(row["consecutive_failures"] or 0)

        # Never succeeded counts as stale: SPEC §6 forbids a failing source from
        # looking like an idle one.
        stale = age_hours is None or age_hours > limit

        if failures >= 3:
            # SPEC §6: after three consecutive failures, warn prominently.
            level = "failing"
        elif stale:
            level = "stale"
        else:
            level = "ok"

        sources.append(
            {
                "source": source,
                "label": SOURCE_LABELS.get(source, source),
                "last_success_at": row["last_success_at"],
                "last_attempt_at": row["last_attempt_at"],
                "last_error": row["last_error"],
                "consecutive_failures": failures,
                "hours_since_success": age_hours,
                "stale_after_hours": limit,
                "level": level,
            }
        )
    return sources


def ingest_summary(conn: sqlite3.Connection) -> dict:
    """Counts behind the banners that ask Mason to do something.

    Each of these is a state SPEC §6 requires be surfaced rather than resolved
    silently: a course the feed named but cannot label, an event whose course could
    not be identified, and an assignment the feed stopped mentioning.
    """
    one = lambda sql: int(conn.execute(sql).fetchone()[0])  # noqa: E731

    return {
        "assignments": one("SELECT COUNT(*) FROM assignments"),
        "from_canvas": one("SELECT COUNT(*) FROM assignments WHERE source = 'ics'"),
        "courses": one("SELECT COUNT(*) FROM courses"),
        "courses_needing_name": one(
            "SELECT COUNT(*) FROM courses WHERE needs_naming = 1"
        ),
        "needs_course": one(
            "SELECT COUNT(*) FROM assignments WHERE course_id IS NULL"
        ),
        "vanished": one(
            "SELECT COUNT(*) FROM assignments WHERE feed_missing_since IS NOT NULL"
        ),
        "canvas_configured": config.canvas_configured(),
    }


def collect(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Everything the status page and /healthz report."""
    now = now or datetime.now(timezone.utc)

    outstanding = [m.filename for m in migrate.pending(conn)]
    sources = sync_sources(conn, now)

    db_bytes = None
    if str(config.DB_PATH) != ":memory:" and config.DB_PATH.exists():
        db_bytes = config.DB_PATH.stat().st_size

    checks = {
        "journal_mode_wal": db.journal_mode(conn) == "wal",
        "fts5": db.fts5_available(conn),
        "migrations_up_to_date": not outstanding,
    }

    return {
        "ok": all(checks.values()),
        "checks": checks,
        "ingest": ingest_summary(conn),
        "schema_version": migrate.current_version(conn),
        "pending_migrations": outstanding,
        "database_path": str(config.DB_PATH),
        "database_bytes": db_bytes,
        "timezone": config.TZ,
        "now": now.strftime(TIMESTAMP_FMT),
        "sync_sources": sources,
        "attention": [s for s in sources if s["level"] != "ok"],
    }
