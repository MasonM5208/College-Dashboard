"""Tests for reminder ladders and quiet hours (SPEC §8, §5).

A reminder at the wrong hour is worse than none — it teaches you to ignore the
ones that are right — so the arithmetic is tested directly rather than through
the push.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import config, db, migrate, reminders

INDIANA = ZoneInfo("America/Indiana/Indianapolis")


def utc(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def local(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=INDIANA)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    migrate.run(path)
    connection = db.connect(path)
    try:
        connection.execute(
            "INSERT INTO terms (id,name,start_date,end_date) "
            "VALUES (1,'FA26','2026-08-24','2026-12-18')"
        )
        connection.execute("INSERT INTO courses (id,term_id,name) VALUES (1,1,'Theory')")
        yield connection
    finally:
        connection.close()


def add(conn, title, due_local, kind="worksheet", start_by=None, status="not_started"):
    cur = conn.execute(
        "INSERT INTO assignments (course_id,title,type,source,due_at,start_by,status) "
        "VALUES (1,?,?,'manual',?,?,?)",
        (title, kind, local(due_local).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
         start_by, status),
    )
    return conn.execute(
        "SELECT * FROM assignments WHERE id = ?", (cur.lastrowid,)
    ).fetchone()


def fire_times(conn, assignment_id):
    return [
        reminders._parse(row["fire_at"]).astimezone(INDIANA)
        for row in conn.execute(
            "SELECT fire_at FROM reminder_instances WHERE assignment_id = ? "
            "AND state = 'pending' ORDER BY fire_at",
            (assignment_id,),
        )
    ]


# --- durations --------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("P1D", timedelta(days=1)),
    ("P7D", timedelta(days=7)),
    ("PT3H", timedelta(hours=3)),
    ("P10D", timedelta(days=10)),
])
def test_offsets_are_read_as_durations(text, expected):
    assert reminders.parse_offset(text) == expected


def test_the_named_rungs_are_not_offsets():
    assert reminders.parse_offset("MORNING_OF") is None
    assert reminders.parse_offset("NIGHT_BEFORE") is None


def test_nonsense_is_refused():
    with pytest.raises(ValueError):
        reminders.parse_offset("soon")


# --- quiet hours ------------------------------------------------------------


@pytest.mark.parametrize("clock,inside", [
    ("2026-09-08 22:29", False),
    ("2026-09-08 22:30", True),
    ("2026-09-08 23:59", True),
    ("2026-09-08 03:00", True),
    ("2026-09-08 07:29", True),
    ("2026-09-08 07:30", False),
    ("2026-09-08 14:00", False),
])
def test_the_quiet_window_wraps_midnight(clock, inside):
    assert reminders.in_quiet_hours(local(clock)) is inside


def test_a_deadline_warning_moves_earlier(clock=None):
    """SPEC §8: due_by shifts to the earlier edge. Late is useless."""
    assert reminders.shift_out_of_quiet_hours(
        local("2026-09-08 23:15"), "due_by"
    ) == local("2026-09-08 22:30")


def test_a_small_hours_warning_moves_to_the_previous_evening():
    """The case worth naming: 22:30 the same day is still ahead of 03:00."""
    shifted = reminders.shift_out_of_quiet_hours(local("2026-09-08 03:00"), "due_by")
    assert shifted == local("2026-09-07 22:30")
    assert shifted < local("2026-09-08 03:00")


def test_a_start_nudge_moves_later():
    """Starting work is a morning activity."""
    assert reminders.shift_out_of_quiet_hours(
        local("2026-09-08 03:00"), "start_by"
    ) == local("2026-09-08 07:30")
    assert reminders.shift_out_of_quiet_hours(
        local("2026-09-08 23:15"), "start_by"
    ) == local("2026-09-09 07:30")


def test_a_reasonable_hour_is_left_alone():
    assert reminders.shift_out_of_quiet_hours(
        local("2026-09-08 14:00"), "due_by"
    ) == local("2026-09-08 14:00")


# --- the ladders, against SPEC §8's table -----------------------------------


def test_a_worksheet_gets_the_day_before_and_a_few_hours_out(conn):
    assignment = add(conn, "Counterpoint 1", "2026-09-08 23:59", "worksheet")
    reminders.generate_for(conn, assignment, INDIANA, utc("2026-08-01T12:00:00Z"))

    times = fire_times(conn, assignment["id"])
    assert len(times) == 2
    # 24h out lands at 23:59, inside quiet hours, so it shifts back to 22:30.
    assert times[0] == local("2026-09-07 22:30")
    # 3h out is 20:59, which is fine.
    assert times[1] == local("2026-09-08 20:59")


def test_an_exam_gets_the_long_runway(conn):
    assignment = add(conn, "Exam 1", "2026-09-16 09:10", "exam")
    reminders.generate_for(conn, assignment, INDIANA, utc("2026-08-01T12:00:00Z"))

    times = fire_times(conn, assignment["id"])
    assert len(times) == 4
    assert times[0] == local("2026-09-06 09:10")   # 10 days
    assert times[1] == local("2026-09-11 09:10")   # 5 days
    assert times[2] == local("2026-09-14 09:10")   # 2 days
    assert times[3] == local("2026-09-15 21:00")   # night before


def test_a_paper_is_told_when_to_start(conn):
    """SPEC §5's start_by rung — the one that stops long work losing to short."""
    start = local("2026-08-27 10:00").astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assignment = add(conn, "Term paper", "2026-09-08 23:59", "paper", start_by=start)
    reminders.generate_for(conn, assignment, INDIANA, utc("2026-08-01T12:00:00Z"))

    kinds = [
        row["kind"] for row in conn.execute(
            "SELECT kind FROM reminder_instances WHERE assignment_id = ? ORDER BY fire_at",
            (assignment["id"],),
        )
    ]
    assert kinds[0] == "start_by"
    assert kinds.count("due_by") == 4     # 7d, 3d, 1d, morning-of


def test_a_performance_gets_weekly_checkpoints(conn):
    assignment = add(conn, "Junior recital", "2026-11-14 19:30", "performance")
    reminders.generate_for(conn, assignment, INDIANA, utc("2026-08-01T12:00:00Z"))

    times = fire_times(conn, assignment["id"])
    assert len(times) == 4
    gaps = {(times[i + 1] - times[i]).days for i in range(3)}
    assert gaps == {7}


def test_every_type_has_a_ladder(conn):
    """SPEC §8 omits 'other'; silence is the worse default, so it gets one."""
    for kind in ("worksheet", "quiz", "other", "paper", "project",
                 "exam", "performance", "milestone"):
        assert reminders.ladder_for(conn, kind), f"{kind} has no ladder"


# --- what must not happen ---------------------------------------------------


def test_rungs_already_past_are_not_created(conn):
    """Something entered three days out should not fire its 10-day nudge now."""
    assignment = add(conn, "Exam 1", "2026-09-16 09:10", "exam")
    reminders.generate_for(conn, assignment, INDIANA, utc("2026-09-13T12:00:00Z"))

    times = fire_times(conn, assignment["id"])
    assert all(t > local("2026-09-13 08:00") for t in times)
    assert len(times) == 2  # only the 2-day and night-before rungs remain


def test_nothing_fires_after_the_deadline(conn):
    assignment = add(conn, "Quiz", "2026-09-08 09:00", "quiz")
    reminders.generate_for(conn, assignment, INDIANA, utc("2026-08-01T12:00:00Z"))
    for when in fire_times(conn, assignment["id"]):
        assert when < local("2026-09-08 09:00")


def test_two_rungs_colliding_produce_one_alert(conn):
    """After the quiet-hours shift, two rungs can land on the same minute."""
    assignment = add(conn, "Thing", "2026-09-08 23:40", "worksheet")
    reminders.generate_for(conn, assignment, INDIANA, utc("2026-08-01T12:00:00Z"))
    times = fire_times(conn, assignment["id"])
    assert len(times) == len(set(times))


def test_finished_work_is_not_given_reminders(conn):
    add(conn, "Done already", "2026-09-08 23:59", "worksheet", status="submitted")
    assert reminders.generate_all(conn, INDIANA, utc("2026-08-01T12:00:00Z")) == 0


def test_an_assignment_gone_from_the_feed_is_not_given_reminders(conn):
    assignment = add(conn, "Vanished", "2026-09-08 23:59")
    conn.execute(
        "UPDATE assignments SET feed_missing_since = '2026-08-02T00:00:00Z' WHERE id = ?",
        (assignment["id"],),
    )
    assert reminders.generate_all(conn, INDIANA, utc("2026-08-01T12:00:00Z")) == 0


def test_an_assignment_with_no_deadline_is_skipped(conn):
    conn.execute(
        "INSERT INTO assignments (course_id,title,type,source) "
        "VALUES (1,'Captured note','other','manual')"
    )
    assert reminders.generate_all(conn, INDIANA, utc("2026-08-01T12:00:00Z")) == 0


# --- a moved deadline -------------------------------------------------------


def test_a_moved_deadline_supersedes_rather_than_mutating(conn):
    """SPEC §5: replace, never move, so the history stays auditable."""
    assignment = add(conn, "Exam 1", "2026-09-16 09:10", "exam")
    reminders.generate_for(conn, assignment, INDIANA, utc("2026-08-01T12:00:00Z"))
    before = fire_times(conn, assignment["id"])

    retired = reminders.supersede_for(conn, assignment["id"])
    conn.execute(
        "UPDATE assignments SET due_at = ? WHERE id = ?",
        (local("2026-09-23 09:10").astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
         assignment["id"]),
    )
    moved = conn.execute(
        "SELECT * FROM assignments WHERE id = ?", (assignment["id"],)
    ).fetchone()
    reminders.generate_for(conn, moved, INDIANA, utc("2026-08-01T12:00:00Z"))

    assert retired == len(before)
    # The old ones are kept, marked, not deleted.
    assert conn.execute(
        "SELECT COUNT(*) n FROM reminder_instances WHERE state = 'superseded'"
    ).fetchone()["n"] == len(before)

    after = fire_times(conn, assignment["id"])
    assert after != before
    # Every new rung sits before the *new* deadline. Note the earliest legitimately
    # falls before the old date — ten days back from 23 September is the 13th.
    assert all(t < local("2026-09-23 09:10") for t in after)
    # The night-before rung followed the exam a week forward.
    assert after[-1] == local("2026-09-22 21:00")


def test_generation_only_runs_where_it_is_missing(conn):
    add(conn, "One", "2026-09-08 23:59")
    add(conn, "Two", "2026-09-09 23:59")

    first = reminders.generate_all(conn, INDIANA, utc("2026-08-01T12:00:00Z"))
    second = reminders.generate_all(conn, INDIANA, utc("2026-08-01T12:00:00Z"))

    assert first == 4
    assert second == 0
