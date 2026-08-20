"""Tests for the capacity model, the timer, calibration and overload (SPEC §9).

SPEC §9's display rules are the reason these are unusually literal: "Never show a
bare score", "always show the inputs alongside the position", and the sort must be
explainable in one sentence. A ranking that is visibly wrong once stops being read,
so the arithmetic is checked against hand-worked examples rather than against
whatever the code currently returns.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import capacity, config, db, migrate, priority

INDIANA = ZoneInfo("America/Indiana/Indianapolis")

# A Monday, mid-morning, so partial-day arithmetic is exercised by default.
MONDAY = datetime(2026, 9, 7, 13, 0, tzinfo=timezone.utc)  # 09:00 local


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "TZ", "America/Indiana/Indianapolis")
    migrate.run(path)
    connection = db.connect(path)
    try:
        connection.execute(
            "INSERT INTO terms (id,name,start_date,end_date) "
            "VALUES (1,'FA26','2026-08-24','2026-12-18')"
        )
        connection.execute(
            "INSERT INTO courses (id,term_id,name,code,penalty_pct_per_day,"
            "current_grade_pct) VALUES (1,1,'Biology 105','BIOL 105',10,88)"
        )
        connection.execute(
            "INSERT INTO courses (id,term_id,name,code,penalty_pct_per_day,"
            "current_grade_pct) VALUES (2,1,'Music Theory','MUS 214',0,94)"
        )
        yield connection
    finally:
        connection.close()


def set_day(conn, weekday, productive, practice=0.0):
    conn.execute(
        "UPDATE capacity_settings SET productive_hours = ?, practice_hours_target = ? "
        "WHERE weekday = ?",
        (productive, practice, weekday),
    )


def add_commitment(conn, weekday, start, end, kind="class", label="Something"):
    conn.execute(
        "INSERT INTO commitments (term_id,label,kind,weekday,start_time,end_time) "
        "VALUES (1,?,?,?,?,?)",
        (label, kind, weekday, start, end),
    )


def add_assignment(conn, **kwargs):
    fields = {
        "course_id": 1, "title": "Something", "type": "worksheet",
        "due_at": "2026-09-10T23:59:00Z", "est_hours": 2.0,
        "est_hours_remaining": 2.0, "status": "not_started", "source": "manual",
        "points_possible": None,
    }
    fields.update(kwargs)
    columns = ", ".join(fields)
    marks = ", ".join("?" * len(fields))
    cur = conn.execute(
        f"INSERT INTO assignments ({columns}) VALUES ({marks})", list(fields.values())
    )
    return int(cur.lastrowid)


# --- the defaults change nothing --------------------------------------------


def test_the_seeded_defaults_match_the_constant_m2_shipped(conn):
    """A milestone that silently re-ranks everything the morning it lands is one
    that gets distrusted before it is understood."""
    due = MONDAY + timedelta(days=3)

    old = priority.available_hours(MONDAY, due, INDIANA)
    new = capacity.available_hours(conn, MONDAY, due, INDIANA)

    assert new == pytest.approx(old, abs=0.01)


def test_every_weekday_is_seeded(conn):
    assert sorted(capacity.settings(conn)) == [0, 1, 2, 3, 4, 5, 6]
    assert all(
        row["productive_hours"] == priority.PRODUCTIVE_HOURS_PER_DAY
        for row in capacity.settings(conn).values()
    )


# --- the day's arithmetic ---------------------------------------------------


def whole_day(conn, weekday=0):
    return capacity.day_capacity(
        date(2026, 9, 7) + timedelta(days=weekday),
        capacity.settings(conn),
        capacity.commitments(conn),
    )


def test_a_day_with_no_commitments_yields_its_budget(conn):
    set_day(conn, 0, productive=5.0)
    assert whole_day(conn).available_hours == 5.0


def test_practice_comes_off_the_top(conn):
    """SPEC §9: practice is capacity consumption, protected before anything is
    ranked against it, because it has no due date and would lose every time."""
    set_day(conn, 0, productive=5.0, practice=2.0)
    day = whole_day(conn)

    assert day.practice_hours == 2.0
    assert day.available_hours == 3.0


def test_practice_larger_than_the_budget_leaves_nothing_not_a_negative(conn):
    set_day(conn, 0, productive=2.0, practice=5.0)
    assert whole_day(conn).available_hours == 0.0


def test_a_heavily_booked_day_is_capped_by_the_clock_not_the_budget(conn):
    """The scarcer of the two governs. 14-hour window, 12 hours booked, so two
    hours are physically free however generous the budget is."""
    set_day(conn, 0, productive=8.0)
    add_commitment(conn, 0, "08:00", "20:00")

    day = whole_day(conn)

    assert day.committed_hours == 12.0
    assert day.unbooked_hours == 2.0
    assert day.available_hours == 2.0


def test_a_lightly_booked_day_is_capped_by_the_budget_not_the_clock(conn):
    set_day(conn, 0, productive=4.0)
    add_commitment(conn, 0, "09:00", "10:00")

    day = whole_day(conn)

    assert day.unbooked_hours == 13.0
    assert day.available_hours == 4.0


def test_overlapping_commitments_are_not_subtracted_twice(conn):
    """A lesson inside a rehearsal block, or the same thing entered twice."""
    set_day(conn, 0, productive=12.0)
    add_commitment(conn, 0, "09:00", "12:00", label="Rehearsal")
    add_commitment(conn, 0, "10:00", "11:00", label="Lesson")

    assert whole_day(conn).committed_hours == 3.0


def test_touching_commitments_merge_without_a_gap(conn):
    set_day(conn, 0, productive=12.0)
    add_commitment(conn, 0, "09:00", "10:00")
    add_commitment(conn, 0, "10:00", "11:00")

    assert whole_day(conn).committed_hours == 2.0


def test_separate_commitments_both_count(conn):
    set_day(conn, 0, productive=12.0)
    add_commitment(conn, 0, "09:00", "10:00")
    add_commitment(conn, 0, "14:00", "16:00")

    assert whole_day(conn).committed_hours == 3.0


def test_commitments_outside_the_working_window_do_not_count(conn):
    """A 6am run and a midnight shift are not competing with coursework hours."""
    set_day(conn, 0, productive=6.0)
    add_commitment(conn, 0, "05:00", "07:00")

    assert whole_day(conn).committed_hours == 0.0
    assert whole_day(conn).available_hours == 6.0


def test_a_commitment_only_partly_inside_the_window_counts_only_that_part(conn):
    set_day(conn, 0, productive=12.0)
    add_commitment(conn, 0, "06:00", "09:00")

    assert whole_day(conn).committed_hours == 1.0


def test_an_inactive_commitment_is_ignored(conn):
    set_day(conn, 0, productive=8.0)
    add_commitment(conn, 0, "08:00", "20:00")
    conn.execute("UPDATE commitments SET active = 0")

    assert whole_day(conn).committed_hours == 0.0


def test_each_weekday_uses_its_own_settings(conn):
    set_day(conn, 0, productive=2.0)   # Monday
    set_day(conn, 5, productive=9.0)   # Saturday

    assert whole_day(conn, weekday=0).available_hours == 2.0
    assert whole_day(conn, weekday=5).available_hours == 9.0


# --- hours between two moments ----------------------------------------------


def test_a_deadline_already_past_yields_zero(conn):
    assert capacity.available_hours(conn, MONDAY, MONDAY - timedelta(hours=1), INDIANA) == 0.0


def test_a_rehearsal_week_leaves_less_time_than_a_free_one(conn):
    due = MONDAY + timedelta(days=4)
    free = capacity.available_hours(conn, MONDAY, due, INDIANA)

    for weekday in range(7):
        add_commitment(conn, weekday, "08:00", "20:00", kind="ensemble")

    booked = capacity.available_hours(conn, MONDAY, due, INDIANA)

    assert booked < free
    assert booked > 0


def test_today_counts_only_the_part_of_it_that_is_left(conn):
    """09:00 local, so most of the day remains; 21:00 and almost none does."""
    late = MONDAY.replace(hour=1) + timedelta(days=1)  # 21:00 local Monday
    due = MONDAY + timedelta(days=2)

    from_morning = capacity.available_hours(conn, MONDAY, due, INDIANA)
    from_evening = capacity.available_hours(conn, late, due, INDIANA)

    assert from_evening < from_morning


# --- ranking through the real model -----------------------------------------


def test_ranking_uses_the_capacity_model_when_it_is_given_one(conn):
    add_assignment(conn, title="Lab report", est_hours=6.0, est_hours_remaining=6.0,
                   due_at="2026-09-09T23:59:00Z")
    rows = conn.execute(
        "SELECT a.*, c.name AS course_name, c.code AS course_code FROM assignments a "
        "LEFT JOIN courses c ON c.id = a.course_id"
    ).fetchall()

    generous = priority.rank(rows, INDIANA, now=MONDAY,
                             available_fn=capacity.ranker(conn))[0]

    # Book the week solid, and the same work no longer fits.
    for weekday in range(7):
        add_commitment(conn, weekday, "08:00", "21:00", kind="ensemble")
    booked = priority.rank(rows, INDIANA, now=MONDAY,
                           available_fn=capacity.ranker(conn))[0]

    assert booked.hours_free < generous.hours_free
    assert booked.slack < generous.slack


def test_practice_can_push_an_assignment_into_negative_slack(conn):
    """The whole point of modelling practice as capacity."""
    add_assignment(conn, title="Paper", est_hours=8.0, est_hours_remaining=8.0,
                   due_at="2026-09-10T23:59:00Z")
    rows = conn.execute(
        "SELECT a.*, c.name AS course_name, c.code AS course_code FROM assignments a "
        "LEFT JOIN courses c ON c.id = a.course_id"
    ).fetchall()

    before = priority.rank(rows, INDIANA, now=MONDAY,
                           available_fn=capacity.ranker(conn))[0]
    assert before.slack > 0

    for weekday in range(7):
        set_day(conn, weekday, productive=4.0, practice=3.5)

    after = priority.rank(rows, INDIANA, now=MONDAY,
                          available_fn=capacity.ranker(conn))[0]
    assert after.slack < 0


# --- the timer --------------------------------------------------------------


def test_starting_a_timer_marks_the_work_in_progress(conn):
    item = add_assignment(conn, status="not_started")

    capacity.start_timer(conn, item, now=MONDAY)

    assert capacity.running(conn)["assignment_id"] == item
    assert conn.execute(
        "SELECT status FROM assignments WHERE id = ?", (item,)
    ).fetchone()["status"] == "in_progress"


def test_stopping_books_the_time_and_reduces_what_is_left(conn):
    """Bookkeeping, not the silent inflation SPEC §9 forbids: ninety minutes
    against a three-hour task leaves an hour and a half."""
    item = add_assignment(conn, est_hours=3.0, est_hours_remaining=3.0)

    capacity.start_timer(conn, item, now=MONDAY)
    capacity.stop_timer(conn, now=MONDAY + timedelta(minutes=90))

    assert capacity.logged_hours(conn, item) == 1.5
    assert conn.execute(
        "SELECT est_hours_remaining FROM assignments WHERE id = ?", (item,)
    ).fetchone()["est_hours_remaining"] == 1.5


def test_the_estimate_itself_is_never_touched_by_the_timer(conn):
    item = add_assignment(conn, est_hours=3.0, est_hours_remaining=3.0)

    capacity.start_timer(conn, item, now=MONDAY)
    capacity.stop_timer(conn, now=MONDAY + timedelta(hours=1))

    assert conn.execute(
        "SELECT est_hours FROM assignments WHERE id = ?", (item,)
    ).fetchone()["est_hours"] == 3.0


def test_working_longer_than_estimated_leaves_nothing_not_a_negative(conn):
    item = add_assignment(conn, est_hours=1.0, est_hours_remaining=1.0)

    capacity.start_timer(conn, item, now=MONDAY)
    capacity.stop_timer(conn, now=MONDAY + timedelta(hours=4))

    assert conn.execute(
        "SELECT est_hours_remaining FROM assignments WHERE id = ?", (item,)
    ).fetchone()["est_hours_remaining"] == 0.0
    # The real four hours are still on the record, which is what calibration reads.
    assert capacity.logged_hours(conn, item) == 4.0


def test_starting_a_second_timer_stops_the_first(conn):
    """Refusing would mean four steps at the moment attention has already moved
    on, which is how a timer stops being used."""
    first = add_assignment(conn, title="First")
    second = add_assignment(conn, title="Second")

    capacity.start_timer(conn, first, now=MONDAY)
    capacity.start_timer(conn, second, now=MONDAY + timedelta(minutes=30))

    assert capacity.running(conn)["assignment_id"] == second
    assert capacity.logged_hours(conn, first) == 0.5


def test_starting_the_timer_already_running_changes_nothing(conn):
    item = add_assignment(conn)
    capacity.start_timer(conn, item, now=MONDAY)
    capacity.start_timer(conn, item, now=MONDAY + timedelta(minutes=10))

    assert conn.execute("SELECT COUNT(*) n FROM time_entries").fetchone()["n"] == 1


def test_stopping_when_nothing_runs_is_harmless(conn):
    assert capacity.stop_timer(conn, now=MONDAY) is None


def test_a_timer_left_running_across_midnight_is_measured_correctly(conn):
    item = add_assignment(conn, est_hours=20.0, est_hours_remaining=20.0)

    capacity.start_timer(conn, item, now=MONDAY.replace(hour=23))
    capacity.stop_timer(conn, now=MONDAY.replace(hour=23) + timedelta(hours=2))

    assert capacity.logged_hours(conn, item) == 2.0


def test_the_database_refuses_two_running_timers(conn):
    """Two open timers would make every calibration figure quietly wrong."""
    import sqlite3

    add_assignment(conn)
    conn.execute(
        "INSERT INTO time_entries (assignment_id, started_at) VALUES (1, '2026-09-07T13:00:00Z')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO time_entries (assignment_id, started_at) "
            "VALUES (1, '2026-09-07T14:00:00Z')"
        )


# --- calibration ------------------------------------------------------------


def timed(conn, assignment_type, estimated, actual, status="submitted"):
    item = add_assignment(conn, type=assignment_type, est_hours=estimated,
                          est_hours_remaining=0.0, status=status)
    conn.execute(
        "INSERT INTO time_entries (assignment_id, started_at, ended_at, minutes) "
        "VALUES (?, '2026-09-07T13:00:00Z', '2026-09-07T15:00:00Z', ?)",
        (item, actual * 60),
    )
    return item


def test_calibration_says_nothing_until_there_is_enough_evidence(conn):
    """Below the threshold a multiplier is noise dressed as data."""
    timed(conn, "paper", 3.0, 6.0)
    timed(conn, "paper", 2.0, 4.0)
    capacity.recalibrate(conn)

    assert capacity.trusted_multiplier(conn, "paper") is None
    assert "not enough yet" in capacity.describe_calibration(
        capacity.calibration(conn)["paper"]
    )


def test_calibration_finds_the_two_times_spec_expects_for_papers(conn):
    """SPEC §9: "Expect the owner to underestimate papers by roughly 2x at first."""
    for _ in range(3):
        timed(conn, "paper", 3.0, 6.0)
    capacity.recalibrate(conn)

    assert capacity.trusted_multiplier(conn, "paper") == pytest.approx(2.0)
    assert "2.0×" in capacity.describe_calibration(capacity.calibration(conn)["paper"])


def test_one_disastrous_afternoon_does_not_move_the_multiplier(conn):
    """The median, not the mean. Something abandoned half-finished at 3am and
    timed for six hours should not re-price every worksheet after it."""
    timed(conn, "worksheet", 1.0, 1.0)
    timed(conn, "worksheet", 1.0, 1.0)
    timed(conn, "worksheet", 1.0, 1.0)
    timed(conn, "worksheet", 1.0, 12.0)
    capacity.recalibrate(conn)

    assert capacity.trusted_multiplier(conn, "worksheet") == pytest.approx(1.0)


def test_unfinished_work_is_not_counted(conn):
    """A part-timed task has logged less than its true cost by definition, and
    counting it would conclude Mason overestimates — the opposite of the truth."""
    for _ in range(3):
        timed(conn, "project", 4.0, 8.0)
    timed(conn, "project", 4.0, 0.5, status="in_progress")
    capacity.recalibrate(conn)

    assert capacity.trusted_multiplier(conn, "project") == pytest.approx(2.0)


def test_types_are_calibrated_separately(conn):
    for _ in range(3):
        timed(conn, "paper", 2.0, 4.0)
    for _ in range(3):
        timed(conn, "quiz", 2.0, 1.0)
    capacity.recalibrate(conn)

    assert capacity.trusted_multiplier(conn, "paper") == pytest.approx(2.0)
    assert capacity.trusted_multiplier(conn, "quiz") == pytest.approx(0.5)


def test_an_accurate_estimator_is_told_so_rather_than_nagged(conn):
    for _ in range(3):
        timed(conn, "quiz", 2.0, 2.0)
    capacity.recalibrate(conn)

    assert "about right" in capacity.describe_calibration(
        capacity.calibration(conn)["quiz"]
    )


def test_stopping_the_timer_recalibrates(conn):
    for _ in range(2):
        timed(conn, "paper", 2.0, 4.0)
    item = add_assignment(conn, type="paper", est_hours=2.0, est_hours_remaining=2.0)

    capacity.start_timer(conn, item, now=MONDAY)
    capacity.stop_timer(conn, now=MONDAY + timedelta(hours=4))
    conn.execute("UPDATE assignments SET status = 'submitted' WHERE id = ?", (item,))
    capacity.recalibrate(conn)

    assert capacity.trusted_multiplier(conn, "paper") == pytest.approx(2.0)


# --- overload mode ----------------------------------------------------------
#
# SPEC §9 calls this "the highest-value feature in this specification after
# reminders themselves", and closes with "Do not soften this. Do not hide it
# behind a toggle. Do not add encouragement."


def soon(days, hour=23):
    return (MONDAY + timedelta(days=days)).replace(hour=hour).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def test_a_week_that_fits_is_not_reported_as_overloaded(conn):
    add_assignment(conn, est_hours=2.0, est_hours_remaining=2.0, due_at=soon(3))

    state = capacity.overload(conn, INDIANA, now=MONDAY)

    assert state.overloaded is False
    assert capacity.recommended_sacrifices(state) == []


def test_more_work_than_hours_is_reported_in_plain_numbers(conn):
    """SPEC §9 step 1: "State the shortfall in plain numbers"."""
    for index in range(5):
        add_assignment(conn, title=f"Item {index}", est_hours=8.0,
                       est_hours_remaining=8.0, due_at=soon(3))

    state = capacity.overload(conn, INDIANA, now=MONDAY)

    assert state.overloaded is True
    assert state.hours_of_work == 40.0
    assert "hours of work" in state.headline and "hours available" in state.headline
    # No bare score anywhere in it (SPEC §9 display rule 1).
    assert "priority" not in state.headline.lower()


def test_work_with_no_estimate_is_left_out_rather_than_assumed_to_be_free(conn):
    """Assuming zero would make an overloaded week look survivable, which is the
    one direction this must never be wrong in."""
    add_assignment(conn, est_hours=None, est_hours_remaining=None, due_at=soon(2))

    state = capacity.overload(conn, INDIANA, now=MONDAY)

    assert state.items == 0
    assert state.hours_of_work == 0.0


def test_finished_work_does_not_count_against_the_week(conn):
    add_assignment(conn, est_hours=20.0, est_hours_remaining=20.0,
                   due_at=soon(2), status="submitted")

    assert capacity.overload(conn, INDIANA, now=MONDAY).hours_of_work == 0.0


def test_work_beyond_the_window_does_not_count(conn):
    add_assignment(conn, est_hours=20.0, est_hours_remaining=20.0, due_at=soon(30))

    assert capacity.overload(conn, INDIANA, now=MONDAY).items == 0


def test_the_course_that_takes_no_late_penalty_is_sacrificed_first(conn):
    """SPEC §9: "Some professors take 10% per day, some take nothing." That is
    usually what decides which thing is genuinely cheap to postpone."""
    add_assignment(conn, course_id=1, title="Bio lab", est_hours=10.0,
                   est_hours_remaining=10.0, due_at=soon(2), points_possible=100)
    add_assignment(conn, course_id=2, title="Theory homework", est_hours=10.0,
                   est_hours_remaining=10.0, due_at=soon(2), points_possible=100)
    for weekday in range(7):
        set_day(conn, weekday, productive=1.0)

    state = capacity.overload(conn, INDIANA, now=MONDAY)

    assert state.overloaded
    assert state.candidates[0].title == "Theory homework"
    assert "no late penalty" in state.candidates[0].reason


def test_an_item_with_no_recorded_cost_is_never_recommended_first(conn):
    """Advising that something be dropped without knowing what it costs is the
    confident-but-wrong advice that would end the feature."""
    add_assignment(conn, course_id=2, title="Known cost", est_hours=10.0,
                   est_hours_remaining=10.0, due_at=soon(2), points_possible=50)
    add_assignment(conn, course_id=None, title="Unknown cost", est_hours=10.0,
                   est_hours_remaining=10.0, due_at=soon(2), points_possible=None)
    for weekday in range(7):
        set_day(conn, weekday, productive=1.0)

    state = capacity.overload(conn, INDIANA, now=MONDAY)

    assert state.candidates[0].title == "Known cost"
    assert state.candidates[-1].title == "Unknown cost"
    assert "unknown" in state.candidates[-1].reason


def test_only_enough_is_recommended_to_close_the_gap(conn):
    """SPEC §9 step 3: "the specific one or two items to let slide". A list of
    everything droppable is a list nobody acts on."""
    for index in range(6):
        add_assignment(conn, course_id=2, title=f"Item {index}", est_hours=5.0,
                       est_hours_remaining=5.0, due_at=soon(3), points_possible=10)
    for weekday in range(7):
        set_day(conn, weekday, productive=3.0)

    state = capacity.overload(conn, INDIANA, now=MONDAY)
    chosen = capacity.recommended_sacrifices(state)

    assert 0 < len(chosen) < 6
    assert sum(item.hours for item in chosen) >= state.shortfall


def test_every_recommendation_carries_its_cost(conn):
    """SPEC §9: "each with its projected grade cost"."""
    add_assignment(conn, course_id=1, title="Bio lab", est_hours=30.0,
                   est_hours_remaining=30.0, due_at=soon(2), points_possible=100)

    state = capacity.overload(conn, INDIANA, now=MONDAY)

    for item in capacity.recommended_sacrifices(state):
        assert item.reason
        assert item.hours > 0


def test_a_per_assignment_penalty_overrides_the_courses(conn):
    add_assignment(conn, course_id=1, title="Special case", est_hours=10.0,
                   est_hours_remaining=10.0, due_at=soon(2), points_possible=100,
                   late_penalty_override=0)
    add_assignment(conn, course_id=1, title="Ordinary", est_hours=10.0,
                   est_hours_remaining=10.0, due_at=soon(2), points_possible=100)
    for weekday in range(7):
        set_day(conn, weekday, productive=1.0)

    state = capacity.overload(conn, INDIANA, now=MONDAY)

    assert state.candidates[0].title == "Special case"


def test_a_booked_week_can_tip_a_survivable_one_into_overload(conn):
    """The capacity model and overload agree with each other, which they must:
    two answers to "how many hours are there" would eventually disagree."""
    for index in range(3):
        add_assignment(conn, title=f"Item {index}", est_hours=6.0,
                       est_hours_remaining=6.0, due_at=soon(5))

    assert capacity.overload(conn, INDIANA, now=MONDAY).overloaded is False

    for weekday in range(7):
        add_commitment(conn, weekday, "08:00", "20:00", kind="ensemble")

    assert capacity.overload(conn, INDIANA, now=MONDAY).overloaded is True
