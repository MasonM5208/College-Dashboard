"""Fetching the Canvas feed and reconciling it against the database.

SPEC §6 governs everything here. The rules that are easy to get wrong:

* **Diff on UID.** The UID is stable across polls, which is what makes a moved
  deadline distinguishable from a new assignment.
* **Never hard-delete.** An event vanishing usually means it was deleted or
  unpublished, but it can also be a transient feed error, and "a transient feed
  error must never destroy data". Vanished items are marked and surfaced.
* **Never drop an unmatched event.** An event whose course cannot be identified is
  stored with no course and shown for review.
* **Never let a failure look like an empty schedule.** Every attempt writes
  sync_state, and three consecutive failures escalate.

One more, from SPEC §11: the feed address is a credential. It is never logged and
never stored in an error message — `sync_state.last_error` is rendered in the
browser, so an unscrubbed exception would publish Mason's Canvas token on a web
page.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app import config, ics, reminders

log = logging.getLogger("canvas")

SOURCE = "canvas_ics"
TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"
USER_AGENT = "SemesterDashboard/1.0 (personal use)"
FETCH_TIMEOUT_SECONDS = 30.0


class FeedError(RuntimeError):
    """The feed could not be fetched. The message is always safe to display."""


@dataclass
class SyncResult:
    fetched: int = 0
    created: int = 0
    retitled: int = 0
    moved: int = 0
    went_missing: int = 0
    came_back: int = 0
    unmatched: int = 0
    courses_created: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.fetched} events: {self.created} new, {self.moved} moved, "
            f"{self.retitled} retitled, {self.went_missing} vanished, "
            f"{self.came_back} returned, {self.unmatched} awaiting a course"
        )


# --- fetching ---------------------------------------------------------------


def _http_message(status: int) -> str:
    if status in (401, 403):
        return (
            f"Canvas refused the calendar feed address (HTTP {status}). The address "
            f"may have been reset — see docs/SECRETS.md for how to get a new one."
        )
    if status == 404:
        return (
            f"Canvas has no calendar at that address (HTTP {status}). Check "
            f"CANVAS_ICS_URL in the secrets file."
        )
    if status >= 500:
        return f"Canvas had a server problem (HTTP {status}). This usually clears on its own."
    return f"Canvas returned an unexpected response (HTTP {status})."


def fetch(url: str, timeout: float = FETCH_TIMEOUT_SECONDS) -> str:
    """Retrieve the feed.

    Every exception is replaced with a fixed description and the original is
    suppressed with `from None`. Both `HTTPError` and `URLError` render the URL
    they failed on, and that URL is a credential.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        raise FeedError(_http_message(exc.code)) from None
    except urllib.error.URLError:
        raise FeedError(
            "Could not reach Canvas. The server may be offline, or its network "
            "connection may be down."
        ) from None
    except TimeoutError:
        raise FeedError(
            f"Canvas did not answer within {int(timeout)} seconds."
        ) from None


# --- sync_state -------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).strftime(TIMESTAMP_FMT)


def record_success(conn: sqlite3.Connection, detail: str) -> None:
    now = _now()
    conn.execute(
        """
        INSERT INTO sync_state (source, last_attempt_at, last_success_at,
                                last_error, cursor, consecutive_failures)
        VALUES (?, ?, ?, NULL, ?, 0)
        ON CONFLICT(source) DO UPDATE SET
          last_attempt_at      = excluded.last_attempt_at,
          last_success_at      = excluded.last_success_at,
          last_error           = NULL,
          cursor               = excluded.cursor,
          consecutive_failures = 0
        """,
        (SOURCE, now, now, detail),
    )


def record_failure(conn: sqlite3.Connection, message: str) -> None:
    """Record a failed attempt, preserving the last success (SPEC §6).

    Keeping last_success_at is what stops a failing feed from reading as "nothing
    due" — the status page can say how stale the data actually is.
    """
    conn.execute(
        """
        INSERT INTO sync_state (source, last_attempt_at, last_success_at,
                                last_error, consecutive_failures)
        VALUES (?, ?, NULL, ?, 1)
        ON CONFLICT(source) DO UPDATE SET
          last_attempt_at      = excluded.last_attempt_at,
          last_error           = excluded.last_error,
          consecutive_failures = sync_state.consecutive_failures + 1
        """,
        (SOURCE, _now(), message),
    )


def _audit(conn: sqlite3.Connection, action: str, record_id: int, detail: dict) -> None:
    conn.execute(
        "INSERT INTO audit_log (action, table_name, record_id, detail_json) "
        "VALUES (?, 'assignments', ?, ?)",
        (action, record_id, json.dumps(detail, separators=(",", ":"))),
    )


# --- courses ----------------------------------------------------------------


def short_code(course_code: str) -> str | None:
    """FA26-BL-MATH-M211-2050 -> MATH-M211, the part a human recognises."""
    parts = course_code.split("-")
    if len(parts) >= 4:
        return f"{parts[2]}-{parts[3]}"
    return None


def _term_id(conn: sqlite3.Connection, name: str, span: tuple[str, str]) -> int:
    row = conn.execute("SELECT id FROM terms WHERE name = ?", (name,)).fetchone()
    if row:
        return int(row["id"])

    # The feed carries no term dates, so these are seeded from the range of events
    # in it and flagged. SPEC §9's display rules turn on never dressing a guess up
    # as a fact.
    cur = conn.execute(
        "INSERT INTO terms (name, start_date, end_date, needs_dates) VALUES (?,?,?,1)",
        (name, span[0], span[1]),
    )
    log.info("Created term %s from the feed, dates need confirming.", name)
    return int(cur.lastrowid)


def resolve_course(
    conn: sqlite3.Connection,
    course_code: str,
    span: tuple[str, str],
    result: SyncResult,
) -> int:
    """Find the course this code belongs to, creating it the first time.

    Auto-creation is the confirmed behaviour: the alternative leaves assignments
    invisible until Mason works through a queue, and the code itself is a
    serviceable placeholder name until he supplies a better one.
    """
    row = conn.execute(
        "SELECT id FROM courses WHERE ics_summary_pattern = ?", (course_code,)
    ).fetchone()
    if row:
        return int(row["id"])

    term_name = ics.term_code(course_code) or "Unsorted"
    term_id = _term_id(conn, term_name, span)

    cur = conn.execute(
        "INSERT INTO courses (term_id, name, code, ics_summary_pattern, needs_naming) "
        "VALUES (?,?,?,?,1)",
        (term_id, course_code, short_code(course_code), course_code),
    )
    result.courses_created.append(course_code)
    log.info("Created course %s from the feed, name needs confirming.", course_code)
    return int(cur.lastrowid)


# --- reconciling ------------------------------------------------------------


def _event_span(due_dates: list[str]) -> tuple[str, str]:
    """A plausible term range from the events seen, as YYYY-MM-DD."""
    if not due_dates:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return today, today
    return min(due_dates)[:10], max(due_dates)[:10]


def reconcile(
    conn: sqlite3.Connection,
    events: list[ics.IcsEvent],
    zone: ZoneInfo,
) -> SyncResult:
    """Apply one poll's worth of events to the database."""
    result = SyncResult(fetched=len(events))

    parsed: list[tuple[ics.IcsEvent, str, str, str | None]] = []
    for event in events:
        title, code = event.title_and_code
        try:
            due_at = ics.due_at_utc(event, zone)
        except ics.IcsError as exc:
            # One unreadable date must not cost the rest of the feed.
            log.warning("Skipping event %s: %s", event.uid, exc)
            continue
        parsed.append((event, title, due_at, code))

    span = _event_span([due for _, _, due, _ in parsed])

    existing = {
        row["ics_uid"]: row
        for row in conn.execute(
            "SELECT id, ics_uid, title, due_at, course_id, feed_missing_since "
            "FROM assignments WHERE source = 'ics' AND ics_uid IS NOT NULL"
        )
    }
    seen: set[str] = set()

    for event, title, due_at, code in parsed:
        seen.add(event.uid)
        course_id = resolve_course(conn, code, span, result) if code else None
        if course_id is None:
            result.unmatched += 1

        row = existing.get(event.uid)

        if row is None:
            cur = conn.execute(
                "INSERT INTO assignments "
                "(course_id, title, type, due_at, status, source, ics_uid) "
                "VALUES (?,?,?,?,'not_started','ics',?)",
                (course_id, title, ics.infer_type(title), due_at, event.uid),
            )
            result.created += 1
            _audit(conn, "ingest_create", int(cur.lastrowid),
                   {"ics_uid": event.uid, "title": title, "due_at": due_at})
            continue

        assignment_id = int(row["id"])
        changes: dict[str, object] = {}

        if row["due_at"] != due_at:
            changes["due_at"] = {"from": row["due_at"], "to": due_at}
            result.moved += 1
        if row["title"] != title:
            changes["title"] = {"from": row["title"], "to": title}
            result.retitled += 1
        if row["course_id"] is None and course_id is not None:
            changes["course_id"] = {"from": None, "to": course_id}
        if row["feed_missing_since"] is not None:
            changes["returned_to_feed"] = True
            result.came_back += 1

        if not changes:
            # Touch nothing, so updated_at keeps meaning "something changed".
            continue

        conn.execute(
            "UPDATE assignments SET title = ?, due_at = ?, "
            "course_id = COALESCE(course_id, ?), feed_missing_since = NULL "
            "WHERE id = ?",
            (title, due_at, course_id, assignment_id),
        )

        if "due_at" in changes:
            # SPEC §5: a moved deadline retires its reminders rather than moving
            # them, so the record of what was scheduled survives. The next sync
            # builds the new ladder and overwrites the to-do on the phone.
            reminders.supersede_for(conn, assignment_id)

        _audit(conn, "ingest_update", assignment_id,
               {"ics_uid": event.uid, "changes": changes})

    # Anything previously ingested that this poll did not mention.
    now = _now()
    for uid, row in existing.items():
        if uid in seen or row["feed_missing_since"] is not None:
            continue
        conn.execute(
            "UPDATE assignments SET feed_missing_since = ? WHERE id = ?",
            (now, int(row["id"])),
        )
        result.went_missing += 1
        _audit(conn, "ingest_vanished", int(row["id"]),
               {"ics_uid": uid, "note": "absent from the feed; not deleted"})

    return result


# --- the whole job ----------------------------------------------------------


def sync(
    conn: sqlite3.Connection,
    *,
    url: str | None = None,
    zone: ZoneInfo | None = None,
) -> SyncResult:
    """Poll the feed once and apply it. Writes sync_state either way."""
    zone = zone or ZoneInfo(config.TZ)

    try:
        feed_url = url or config.require("CANVAS_ICS_URL")
    except config.MissingSetting as exc:
        record_failure(conn, "The Canvas feed address is not configured.")
        raise FeedError(str(exc)) from None

    try:
        text = fetch(feed_url)
        events = ics.parse_events(text)
    except (FeedError, ics.IcsError) as exc:
        record_failure(conn, str(exc))
        log.warning("Canvas sync failed: %s", exc)
        raise FeedError(str(exc)) from None

    try:
        conn.execute("BEGIN")
        result = reconcile(conn, events, zone)
        conn.execute("COMMIT")
    except Exception as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        record_failure(conn, "The feed was read but could not be saved.")
        log.exception("Canvas sync could not be saved")
        raise FeedError("The feed was read but could not be saved.") from None

    record_success(conn, result.summary())
    log.info("Canvas sync: %s", result.summary())
    return result
