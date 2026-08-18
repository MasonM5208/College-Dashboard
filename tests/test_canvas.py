"""Tests for fetching and reconciling the Canvas feed (SPEC §6)."""

from __future__ import annotations

import urllib.error
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import canvas, db, ics, migrate

FIXTURE = Path(__file__).parent / "fixtures" / "canvas_sample.ics"
INDIANA = ZoneInfo("America/Indiana/Indianapolis")

# A stand-in for the real feed address, which is a credential. Tests assert this
# string never escapes into an error message or the database.
SECRET_URL = "https://canvas.example.edu/feeds/calendars/user_SECRETTOKEN123.ics"


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "test.db"
    migrate.run(path)
    connection = db.connect(path)
    yield connection
    connection.close()


@pytest.fixture
def feed_events() -> list[ics.IcsEvent]:
    return ics.parse_events(FIXTURE.read_text(encoding="utf-8"))


def sync_from(conn, events):
    return canvas.reconcile(conn, events, INDIANA)


def rows(conn, sql, *args):
    return conn.execute(sql, args).fetchall()


# --- first ingest -----------------------------------------------------------


def test_first_poll_creates_assignments_courses_and_a_term(conn, feed_events):
    result = sync_from(conn, feed_events)

    assert result.fetched == 7
    assert result.created == 7

    assert rows(conn, "SELECT COUNT(*) n FROM assignments")[0]["n"] == 7
    # Three distinct course codes appear in the fixture.
    assert rows(conn, "SELECT COUNT(*) n FROM courses")[0]["n"] == 3
    # All three share the FA26 prefix, so one term.
    terms = rows(conn, "SELECT name, needs_dates FROM terms")
    assert len(terms) == 1
    assert terms[0]["name"] == "FA26"
    assert terms[0]["needs_dates"] == 1


def test_created_courses_are_flagged_for_naming(conn, feed_events):
    sync_from(conn, feed_events)
    course = rows(
        conn,
        "SELECT name, code, ics_summary_pattern, needs_naming FROM courses "
        "WHERE ics_summary_pattern = 'FA26-XX-STAT-S200-22222'",
    )[0]
    assert course["needs_naming"] == 1
    assert course["name"] == "FA26-XX-STAT-S200-22222"
    assert course["code"] == "STAT-S200"


def test_the_term_span_comes_from_the_events(conn, feed_events):
    sync_from(conn, feed_events)
    term = rows(conn, "SELECT start_date, end_date FROM terms")[0]
    assert term["start_date"] == "2026-08-25"
    assert term["end_date"] == "2026-10-15"


def test_an_event_with_no_course_code_is_kept_for_review(conn, feed_events):
    """SPEC §6.5: never silently dropped."""
    result = sync_from(conn, feed_events)
    assert result.unmatched == 1

    unmatched = rows(
        conn, "SELECT title, due_at FROM assignments WHERE course_id IS NULL"
    )
    assert len(unmatched) == 1
    assert unmatched[0]["title"] == "Departmental recital attendance"
    assert unmatched[0]["due_at"] is not None


def test_types_are_inferred_where_unambiguous(conn, feed_events):
    sync_from(conn, feed_events)
    by_title = {r["title"]: r["type"] for r in rows(conn, "SELECT title, type FROM assignments")}
    assert by_title["Exam 1 Attempt 1 Sec 1.1 - 2.7"] == "exam"
    assert by_title["Quiz on lab safety"] == "quiz"
    assert by_title["Reading response one"] == "other"


def test_estimated_hours_are_left_empty(conn, feed_events):
    """SPEC §9: the owner supplies these. Inventing them poisons the ranking."""
    sync_from(conn, feed_events)
    assert rows(
        conn, "SELECT COUNT(*) n FROM assignments WHERE est_hours IS NOT NULL"
    )[0]["n"] == 0


# --- repeat polls -----------------------------------------------------------


def test_an_identical_second_poll_changes_nothing(conn, feed_events):
    sync_from(conn, feed_events)
    before = rows(conn, "SELECT id, title, due_at, updated_at FROM assignments ORDER BY id")
    audit_before = rows(conn, "SELECT COUNT(*) n FROM audit_log")[0]["n"]

    result = sync_from(conn, feed_events)

    assert (result.created, result.moved, result.retitled) == (0, 0, 0)
    after = rows(conn, "SELECT id, title, due_at, updated_at FROM assignments ORDER BY id")
    assert [dict(r) for r in before] == [dict(r) for r in after]
    # No audit noise either.
    assert rows(conn, "SELECT COUNT(*) n FROM audit_log")[0]["n"] == audit_before


def test_no_duplicate_courses_on_a_second_poll(conn, feed_events):
    sync_from(conn, feed_events)
    result = sync_from(conn, feed_events)
    assert result.courses_created == []
    assert rows(conn, "SELECT COUNT(*) n FROM courses")[0]["n"] == 3


# --- a moved deadline -------------------------------------------------------


def _move(events, uid_tail, new_dtstart):
    moved = []
    for e in events:
        if e.uid.endswith(uid_tail):
            moved.append(
                ics.IcsEvent(uid=e.uid, summary=e.summary, dtstart=new_dtstart,
                             all_day=False, tzid=e.tzid, url=e.url)
            )
        else:
            moved.append(e)
    return moved


def test_a_moved_due_date_is_detected_and_recorded(conn, feed_events):
    """SPEC §12's M1 criterion: a change in Canvas is caught in one poll cycle."""
    sync_from(conn, feed_events)
    result = sync_from(conn, _move(feed_events, "1000003", "20260918T131000Z"))

    assert result.moved == 1
    row = rows(
        conn, "SELECT due_at FROM assignments WHERE ics_uid LIKE '%1000003'"
    )[0]
    assert row["due_at"] == "2026-09-18T13:10:00Z"

    entry = rows(
        conn,
        "SELECT detail_json FROM audit_log WHERE action = 'ingest_update' ORDER BY id DESC",
    )[0]
    assert "2026-09-16T13:10:00Z" in entry["detail_json"]
    assert "2026-09-18T13:10:00Z" in entry["detail_json"]


def test_a_moved_deadline_supersedes_its_reminders(conn, feed_events):
    """SPEC §5: supersede and regenerate; never mutate fire_at in place."""
    sync_from(conn, feed_events)
    assignment_id = rows(
        conn, "SELECT id FROM assignments WHERE ics_uid LIKE '%1000003'"
    )[0]["id"]
    conn.execute(
        "INSERT INTO reminder_instances (assignment_id, kind, fire_at, channel, state) "
        "VALUES (?, 'due_by', '2026-09-15T13:10:00Z', 'caldav', 'pending')",
        (assignment_id,),
    )
    conn.execute(
        "INSERT INTO reminder_instances (assignment_id, kind, fire_at, channel, state) "
        "VALUES (?, 'due_by', '2026-09-01T13:10:00Z', 'caldav', 'sent')",
        (assignment_id,),
    )

    sync_from(conn, _move(feed_events, "1000003", "20260918T131000Z"))

    states = [
        r["state"]
        for r in rows(
            conn,
            "SELECT state FROM reminder_instances WHERE assignment_id = ? ORDER BY id",
            assignment_id,
        )
    ]
    # Both are superseded, and this is the point. Under M3 a 'sent' instance means
    # its alarm is sitting on the phone inside that assignment's to-do — so a moved
    # deadline makes it wrong, and leaving it would fire an alert at the old time.
    # Nothing is lost: superseded rows are kept, which is what SPEC §5 means by the
    # history staying auditable.
    assert states == ["superseded", "superseded"]


def test_a_retitled_event_updates_in_place(conn, feed_events):
    sync_from(conn, feed_events)
    renamed = [
        ics.IcsEvent(uid=e.uid, summary="Renamed thing [FA26-XX-STAT-S200-22222]",
                     dtstart=e.dtstart, all_day=e.all_day, tzid=e.tzid)
        if e.uid.endswith("1000003") else e
        for e in feed_events
    ]
    result = sync_from(conn, renamed)
    assert result.retitled == 1
    assert rows(
        conn, "SELECT title FROM assignments WHERE ics_uid LIKE '%1000003'"
    )[0]["title"] == "Renamed thing"


# --- vanishing and returning ------------------------------------------------


def test_an_event_that_vanishes_is_marked_never_deleted(conn, feed_events):
    """SPEC §6.6: a transient feed error must never destroy data."""
    sync_from(conn, feed_events)
    remaining = [e for e in feed_events if not e.uid.endswith("1000003")]

    result = sync_from(conn, remaining)

    assert result.went_missing == 1
    assert rows(conn, "SELECT COUNT(*) n FROM assignments")[0]["n"] == 7
    row = rows(
        conn, "SELECT feed_missing_since FROM assignments WHERE ics_uid LIKE '%1000003'"
    )[0]
    assert row["feed_missing_since"] is not None


def test_a_vanished_event_is_only_flagged_once(conn, feed_events):
    sync_from(conn, feed_events)
    remaining = [e for e in feed_events if not e.uid.endswith("1000003")]
    first = sync_from(conn, remaining)
    second = sync_from(conn, remaining)
    assert first.went_missing == 1
    assert second.went_missing == 0


def test_an_event_that_comes_back_is_unflagged(conn, feed_events):
    sync_from(conn, feed_events)
    sync_from(conn, [e for e in feed_events if not e.uid.endswith("1000003")])

    result = sync_from(conn, feed_events)

    assert result.came_back == 1
    row = rows(
        conn, "SELECT feed_missing_since FROM assignments WHERE ics_uid LIKE '%1000003'"
    )[0]
    assert row["feed_missing_since"] is None


# --- failure handling -------------------------------------------------------


def test_a_failed_fetch_records_the_failure_and_keeps_the_last_success(conn, feed_events, monkeypatch):
    sync_from(conn, feed_events)
    canvas.record_success(conn, "7 events")
    good = rows(conn, "SELECT last_success_at FROM sync_state")[0]["last_success_at"]

    def boom(url, timeout=30.0):
        raise canvas.FeedError("Canvas had a server problem (HTTP 503).")

    monkeypatch.setattr(canvas, "fetch", boom)
    with pytest.raises(canvas.FeedError):
        canvas.sync(conn, url=SECRET_URL, zone=INDIANA)

    state = rows(conn, "SELECT * FROM sync_state WHERE source = 'canvas_ics'")[0]
    assert state["consecutive_failures"] == 1
    # SPEC §6: a failure must never read as "nothing due".
    assert state["last_success_at"] == good
    assert "503" in state["last_error"]


def test_consecutive_failures_escalate(conn, monkeypatch):
    monkeypatch.setattr(
        canvas, "fetch",
        lambda url, timeout=30.0: (_ for _ in ()).throw(canvas.FeedError("down")),
    )
    for _ in range(3):
        with pytest.raises(canvas.FeedError):
            canvas.sync(conn, url=SECRET_URL, zone=INDIANA)
    assert rows(conn, "SELECT consecutive_failures n FROM sync_state")[0]["n"] == 3


def test_a_success_clears_the_failure_count(conn, feed_events, monkeypatch):
    monkeypatch.setattr(
        canvas, "fetch",
        lambda url, timeout=30.0: (_ for _ in ()).throw(canvas.FeedError("down")),
    )
    with pytest.raises(canvas.FeedError):
        canvas.sync(conn, url=SECRET_URL, zone=INDIANA)

    monkeypatch.setattr(
        canvas, "fetch", lambda url, timeout=30.0: FIXTURE.read_text(encoding="utf-8")
    )
    canvas.sync(conn, url=SECRET_URL, zone=INDIANA)

    state = rows(conn, "SELECT * FROM sync_state")[0]
    assert state["consecutive_failures"] == 0
    assert state["last_error"] is None


def test_an_html_error_page_is_not_mistaken_for_an_empty_calendar(conn, monkeypatch):
    """The dangerous case: Canvas serving HTML would otherwise read as zero events,
    which would mark every assignment as vanished."""
    monkeypatch.setattr(
        canvas, "fetch", lambda url, timeout=30.0: "<html><body>Unauthorized</body></html>"
    )
    with pytest.raises(canvas.FeedError):
        canvas.sync(conn, url=SECRET_URL, zone=INDIANA)
    assert rows(conn, "SELECT COUNT(*) n FROM assignments")[0]["n"] == 0


# --- the feed address is a credential ---------------------------------------


def test_the_feed_url_never_reaches_the_database(conn, monkeypatch):
    """SPEC §11: sync_state.last_error is rendered in the browser."""
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(
                            urllib.error.HTTPError(SECRET_URL, 403, "Forbidden", {}, None)))

    with pytest.raises(canvas.FeedError) as caught:
        canvas.sync(conn, url=SECRET_URL, zone=INDIANA)

    assert "SECRETTOKEN123" not in str(caught.value)
    for row in rows(conn, "SELECT last_error FROM sync_state"):
        assert "SECRETTOKEN123" not in (row["last_error"] or "")
        assert "canvas.example.edu" not in (row["last_error"] or "")


def test_the_feed_url_never_reaches_the_logs(conn, monkeypatch, caplog):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(
                            urllib.error.URLError("nodename nor servname provided")))

    with caplog.at_level("DEBUG"):
        with pytest.raises(canvas.FeedError):
            canvas.sync(conn, url=SECRET_URL, zone=INDIANA)

    assert "SECRETTOKEN123" not in caplog.text


def test_http_messages_name_the_status_and_carry_no_address():
    for status in (401, 403, 404, 500, 503):
        message = canvas._http_message(status)
        assert str(status) in message
        assert "://" not in message
