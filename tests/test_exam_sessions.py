"""Tests for exam study sessions (SPEC §9's supporting features).

"An exam 10 days out generates study sessions with estimated hours, which then
compete for capacity like any other work."

The risk in a feature that creates work on its own is that it becomes a nuisance
and gets ignored, taking the rest of the ranking's credibility with it. Most of
these check the guard rails rather than the generation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import capacity, config, db, migrate

INDIANA = ZoneInfo("America/Indiana/Indianapolis")
NOW = datetime(2026, 9, 7, 13, 0, tzinfo=timezone.utc)


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
        connection.execute("INSERT INTO courses (id,term_id,name) VALUES (1,1,'Biology')")
        yield connection
    finally:
        connection.close()


def add_exam(conn, days_out=14, hours=8.0, title="Midterm"):
    due = (NOW + timedelta(days=days_out)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = conn.execute(
        "INSERT INTO assignments (course_id,title,type,due_at,est_hours,"
        "est_hours_remaining,status,source) VALUES (1,?,'exam',?,?,?,'not_started','manual')",
        (title, due, hours, hours),
    )
    return int(cur.lastrowid)


def sessions(conn, exam_id):
    return conn.execute(
        "SELECT * FROM assignments WHERE parent_assignment_id = ? AND id <> ? "
        "ORDER BY due_at",
        (exam_id, exam_id),
    ).fetchall()


# --- generating -------------------------------------------------------------


def test_an_exam_far_enough_out_gets_sessions(conn):
    exam = add_exam(conn, days_out=14)

    assert capacity.generate_study_sessions(conn, INDIANA, now=NOW) == 4

    rows = sessions(conn, exam)
    assert len(rows) == 4
    assert all(row["type"] == "milestone" for row in rows)
    assert all(row["course_id"] == 1 for row in rows)


def test_the_sessions_all_fall_before_the_exam(conn):
    exam = add_exam(conn, days_out=14)
    capacity.generate_study_sessions(conn, INDIANA, now=NOW)

    exam_due = conn.execute(
        "SELECT due_at FROM assignments WHERE id = ?", (exam,)
    ).fetchone()["due_at"]
    assert all(row["due_at"] < exam_due for row in sessions(conn, exam))


def test_the_exams_estimate_is_split_across_the_sessions(conn):
    """SPEC: sessions carry estimated hours, so they compete for capacity."""
    exam = add_exam(conn, days_out=14, hours=8.0)
    capacity.generate_study_sessions(conn, INDIANA, now=NOW)

    rows = sessions(conn, exam)
    assert sum(row["est_hours"] for row in rows) == pytest.approx(8.0, abs=0.05)


def test_sessions_are_named_so_they_are_recognisable_in_a_list(conn):
    exam = add_exam(conn, days_out=14, title="Genetics midterm")
    capacity.generate_study_sessions(conn, INDIANA, now=NOW)

    titles = [row["title"] for row in sessions(conn, exam)]
    assert "Study for Genetics midterm (1 of 4)" in titles
    assert "Study for Genetics midterm (4 of 4)" in titles


def test_an_exam_too_close_generates_nothing(conn):
    """Three study sessions inside four days is busywork competing with the
    revision it is supposed to represent."""
    add_exam(conn, days_out=3)
    assert capacity.generate_study_sessions(conn, INDIANA, now=NOW) == 0


def test_an_exam_with_no_estimate_generates_nothing(conn):
    """Inventing an estimate for revision is exactly the guessing the rest of the
    dashboard refuses to do — and it makes generation something he opts into."""
    conn.execute(
        "INSERT INTO assignments (course_id,title,type,due_at,status,source) "
        "VALUES (1,'Unestimated','exam','2026-09-30T23:59:00Z','not_started','manual')"
    )
    assert capacity.generate_study_sessions(conn, INDIANA, now=NOW) == 0


def test_only_offsets_still_in_the_future_are_used(conn):
    """An exam seven days out gets three sessions, not four with one overdue —
    the eight-days-before rung is already in the past."""
    exam = add_exam(conn, days_out=7)
    capacity.generate_study_sessions(conn, INDIANA, now=NOW)

    rows = sessions(conn, exam)
    assert len(rows) == 3
    assert all(row["due_at"] > NOW.strftime("%Y-%m-%dT%H:%M:%SZ") for row in rows)


def test_a_finished_exam_generates_nothing(conn):
    exam = add_exam(conn, days_out=14)
    conn.execute("UPDATE assignments SET status = 'submitted' WHERE id = ?", (exam,))

    assert capacity.generate_study_sessions(conn, INDIANA, now=NOW) == 0


def test_running_it_twice_does_not_duplicate(conn):
    exam = add_exam(conn, days_out=14)
    capacity.generate_study_sessions(conn, INDIANA, now=NOW)
    capacity.generate_study_sessions(conn, INDIANA, now=NOW)

    assert len(sessions(conn, exam)) == 4


# --- the guard rails --------------------------------------------------------


def test_deleting_the_sessions_makes_them_stay_deleted(conn):
    """"No" has to be a decision that sticks, not one repeated every fifteen
    minutes."""
    exam = add_exam(conn, days_out=14)
    capacity.generate_study_sessions(conn, INDIANA, now=NOW)

    assert capacity.drop_study_sessions(conn, exam) == 4
    capacity.generate_study_sessions(conn, INDIANA, now=NOW)

    assert sessions(conn, exam) == []


def test_work_already_started_is_never_removed(conn):
    exam = add_exam(conn, days_out=14)
    capacity.generate_study_sessions(conn, INDIANA, now=NOW)
    first = sessions(conn, exam)[0]["id"]
    conn.execute("UPDATE assignments SET status = 'in_progress' WHERE id = ?", (first,))

    capacity.drop_study_sessions(conn, exam)

    remaining = [row["id"] for row in sessions(conn, exam)]
    assert remaining == [first]


def test_a_moved_exam_rebuilds_untouched_sessions(conn):
    exam = add_exam(conn, days_out=14)
    capacity.generate_study_sessions(conn, INDIANA, now=NOW)
    before = [row["due_at"] for row in sessions(conn, exam)]

    later = (NOW + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("UPDATE assignments SET due_at = ? WHERE id = ?", (later, exam))
    capacity.generate_study_sessions(conn, INDIANA, now=NOW)

    after = [row["due_at"] for row in sessions(conn, exam)]
    assert after != before
    assert all(row > max(before) for row in after)


def test_a_moved_exam_leaves_started_sessions_alone(conn):
    """Work that was done is a fact, whatever the calendar did afterwards."""
    exam = add_exam(conn, days_out=14)
    capacity.generate_study_sessions(conn, INDIANA, now=NOW)
    first = sessions(conn, exam)[0]
    conn.execute("UPDATE assignments SET status = 'in_progress' WHERE id = ?",
                 (first["id"],))

    later = (NOW + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("UPDATE assignments SET due_at = ? WHERE id = ?", (later, exam))
    capacity.generate_study_sessions(conn, INDIANA, now=NOW)

    kept = conn.execute(
        "SELECT due_at, status FROM assignments WHERE id = ?", (first["id"],)
    ).fetchone()
    assert kept["due_at"] == first["due_at"]
    assert kept["status"] == "in_progress"


def test_the_sessions_count_against_capacity_like_anything_else(conn):
    """SPEC's actual requirement: "which then compete for capacity like any other
    work". They are ordinary assignment rows, so this follows — but it is the
    reason the design is what it is, so it is asserted."""
    conn.execute("UPDATE capacity_settings SET productive_hours = 1.0")
    add_exam(conn, days_out=8, hours=20.0)

    before = capacity.overload(conn, INDIANA, now=NOW).hours_of_work
    capacity.generate_study_sessions(conn, INDIANA, now=NOW)
    after = capacity.overload(conn, INDIANA, now=NOW).hours_of_work

    assert after > before


def test_generation_is_audited(conn):
    add_exam(conn, days_out=14)
    capacity.generate_study_sessions(conn, INDIANA, now=NOW)

    actions = [
        row["action"] for row in conn.execute("SELECT action FROM audit_log")
    ]
    assert "study_sessions" in actions
