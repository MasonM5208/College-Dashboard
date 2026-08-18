"""Tests for slack ranking (SPEC §9).

The arithmetic is tested directly because the display rules in that section turn
on it being right: an item shows the numbers that placed it, so a wrong number is
visible immediately and ends trust in the whole ordering.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app import priority

INDIANA = ZoneInfo("America/Indiana/Indianapolis")


def utc(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def row(**kwargs):
    base = {
        "id": 1,
        "title": "Something",
        "course_name": "Calculus",
        "course_code": "MATH-M211",
        "type": "other",
        "status": "not_started",
        "due_at": None,
        "est_hours": None,
        "est_hours_remaining": None,
        "pinned": 0,
    }
    base.update(kwargs)
    return base


# --- available hours --------------------------------------------------------


def test_a_full_day_yields_the_daily_budget():
    """08:00 to 22:00 on one day is the whole 4-hour budget."""
    hours = priority.available_hours(
        utc("2026-09-01T12:00:00Z"),  # 08:00 Indiana
        utc("2026-09-02T02:00:00Z"),  # 22:00 Indiana
        INDIANA,
    )
    assert hours == pytest.approx(4.0)


def test_half_a_day_yields_half_the_budget():
    hours = priority.available_hours(
        utc("2026-09-01T12:00:00Z"),  # 08:00 local
        utc("2026-09-01T19:00:00Z"),  # 15:00 local, half of the 8-22 window
        INDIANA,
    )
    assert hours == pytest.approx(2.0)


def test_time_outside_the_waking_window_does_not_count():
    """Midnight to 6am is not productive time, and pretending otherwise would
    quietly inflate every estimate of what fits."""
    hours = priority.available_hours(
        utc("2026-09-01T04:00:00Z"),  # midnight local
        utc("2026-09-01T10:00:00Z"),  # 06:00 local
        INDIANA,
    )
    assert hours == 0.0


def test_hours_accumulate_across_days_including_the_weekend():
    """Confirmed decision: weekends count. 2026-09-04 is a Friday."""
    hours = priority.available_hours(
        utc("2026-09-04T19:00:00Z"),  # Friday 15:00 local
        utc("2026-09-07T13:00:00Z"),  # Monday 09:00 local
        INDIANA,
    )
    # Friday 15:00-22:00 = 7/14 of a day, Sat and Sun whole, Monday 08:00-09:00.
    expected = (7 / 14 + 1 + 1 + 1 / 14) * priority.PRODUCTIVE_HOURS_PER_DAY
    assert hours == pytest.approx(expected, abs=0.01)


def test_a_deadline_already_past_gives_no_time():
    hours = priority.available_hours(
        utc("2026-09-10T12:00:00Z"), utc("2026-09-01T12:00:00Z"), INDIANA
    )
    assert hours == 0.0


# --- the ordering SPEC §9 exists to produce ---------------------------------


def test_a_large_paper_outranks_a_worksheet_due_sooner():
    """SPEC §9's purpose: deadline order is not the same as urgency order.

    Note that SPEC's own illustration — a 20-minute worksheet due tomorrow against
    a 6-hour paper due Thursday — does not actually invert under the formula SPEC
    specifies: the worksheet has 7.1h available for 0.3h of work (6.8h spare) and
    the paper 15.1h for 6h (9.1h spare), so the worksheet is genuinely the tighter
    of the two. The inversion appears once the larger item is big enough relative
    to the time available for it, which is the case that costs grades.
    """
    now = utc("2026-09-01T13:00:00Z")  # Tuesday 09:00 local
    items = priority.rank(
        [
            row(id=1, title="Worksheet", due_at="2026-09-02T23:59:00Z",
                est_hours_remaining=0.33),
            row(id=2, title="Paper", due_at="2026-09-04T23:59:00Z",
                est_hours_remaining=14.0),
        ],
        INDIANA,
        now,
    )
    # A due-date sort would put the worksheet first. Slack does not.
    assert [i.title for i in items] == ["Paper", "Worksheet"]
    assert items[0].slack < items[1].slack


def test_a_small_task_due_imminently_is_still_allowed_to_win():
    """The honest converse, so the ranking is not mistaken for "big things first".

    When the larger item comfortably fits in the time before it is due, the nearer
    deadline is the more constrained one and belongs on top.
    """
    now = utc("2026-09-01T13:00:00Z")
    items = priority.rank(
        [
            row(id=1, title="Worksheet", due_at="2026-09-02T23:59:00Z",
                est_hours_remaining=0.33),
            row(id=2, title="Roomy paper", due_at="2026-09-04T23:59:00Z",
                est_hours_remaining=6.0),
        ],
        INDIANA,
        now,
    )
    assert [i.title for i in items] == ["Worksheet", "Roomy paper"]


def test_negative_slack_means_already_behind():
    now = utc("2026-09-01T13:00:00Z")
    items = priority.rank(
        [row(id=1, title="Impossible", due_at="2026-09-02T13:00:00Z",
             est_hours_remaining=20.0)],
        INDIANA,
        now,
    )
    assert items[0].slack < 0
    assert "short of the time needed" in priority.describe_slack(items[0])


def test_a_pinned_item_always_wins():
    """SPEC §9: the manual override beats the arithmetic."""
    now = utc("2026-09-01T13:00:00Z")
    items = priority.rank(
        [
            row(id=1, title="Tight", due_at="2026-09-02T13:00:00Z",
                est_hours_remaining=8.0),
            row(id=2, title="Pinned", due_at="2026-12-01T13:00:00Z",
                est_hours_remaining=0.5, pinned=1),
        ],
        INDIANA,
        now,
    )
    assert items[0].title == "Pinned"


def test_being_in_progress_is_a_nudge_not_a_takeover():
    """SPEC §9: "Small. Reduces context-switch churn; must never dominate slack.\""""
    now = utc("2026-09-01T13:00:00Z")

    # Nearly tied on slack: the started one should come first.
    close = priority.rank(
        [
            row(id=1, title="Untouched", due_at="2026-09-03T13:00:00Z",
                est_hours_remaining=2.0),
            row(id=2, title="Started", due_at="2026-09-03T13:00:00Z",
                est_hours_remaining=1.8, status="in_progress"),
        ],
        INDIANA, now,
    )
    assert close[0].title == "Started"

    # Far apart: slack must still win.
    apart = priority.rank(
        [
            row(id=1, title="Urgent", due_at="2026-09-02T13:00:00Z",
                est_hours_remaining=6.0),
            row(id=2, title="Started", due_at="2026-12-01T13:00:00Z",
                est_hours_remaining=1.0, status="in_progress"),
        ],
        INDIANA, now,
    )
    assert apart[0].title == "Urgent"


def test_finished_work_leaves_the_ranking():
    now = utc("2026-09-01T13:00:00Z")
    items = priority.rank(
        [
            row(id=1, title="Done", due_at="2026-09-02T13:00:00Z",
                est_hours_remaining=1.0, status="submitted"),
            row(id=2, title="Graded", due_at="2026-09-02T13:00:00Z",
                est_hours_remaining=1.0, status="graded"),
            row(id=3, title="Dismissed", due_at="2026-09-02T13:00:00Z",
                est_hours_remaining=1.0, status="dismissed"),
            row(id=4, title="Live", due_at="2026-09-02T13:00:00Z",
                est_hours_remaining=1.0),
        ],
        INDIANA, now,
    )
    assert [i.title for i in items] == ["Live"]


# --- what cannot be ranked --------------------------------------------------


def test_an_item_with_no_estimate_is_flagged_not_guessed():
    """SPEC §9 forbids inventing the number the whole engine reads."""
    now = utc("2026-09-01T13:00:00Z")
    items = priority.rank(
        [row(id=1, title="No estimate", due_at="2026-09-05T13:00:00Z")],
        INDIANA, now,
    )
    assert items[0].needs_estimate is True
    assert items[0].slack is None
    assert items[0].rankable is False
    assert "needs a time estimate" in priority.describe_slack(items[0])


def test_an_item_with_no_due_date_cannot_be_ranked_either():
    items = priority.rank(
        [row(id=1, title="Captured thought", est_hours_remaining=1.0)],
        INDIANA, utc("2026-09-01T13:00:00Z"),
    )
    assert items[0].slack is None
    assert items[0].hours_free is None


def test_unrankable_items_sort_below_ranked_ones():
    now = utc("2026-09-01T13:00:00Z")
    items = priority.rank(
        [
            row(id=1, title="Unknown"),
            row(id=2, title="Known", due_at="2026-12-01T13:00:00Z",
                est_hours_remaining=1.0),
        ],
        INDIANA, now,
    )
    assert [i.title for i in items] == ["Known", "Unknown"]


def test_est_hours_is_used_when_remaining_is_absent():
    now = utc("2026-09-01T13:00:00Z")
    items = priority.rank(
        [row(id=1, due_at="2026-09-05T13:00:00Z", est_hours=3.0)],
        INDIANA, now,
    )
    assert items[0].hours_left == 3.0
    assert items[0].slack is not None


# --- the numbers shown to Mason ---------------------------------------------


def test_every_ranked_item_carries_its_inputs():
    """SPEC §9 display rule 2: always show the numbers that placed it."""
    now = utc("2026-09-01T13:00:00Z")
    item = priority.rank(
        [row(id=1, due_at="2026-09-03T23:59:00Z", est_hours_remaining=2.5)],
        INDIANA, now,
    )[0]
    assert item.hours_left == 2.5
    assert item.hours_free is not None and item.hours_free > 0
    assert item.slack == pytest.approx(item.hours_free - item.hours_left, abs=0.01)


def test_overdue_items_are_marked():
    now = utc("2026-09-10T13:00:00Z")
    item = priority.rank(
        [row(id=1, due_at="2026-09-01T13:00:00Z", est_hours_remaining=1.0)],
        INDIANA, now,
    )[0]
    assert item.overdue is True
    assert item.hours_free == 0.0
    assert priority.describe_slack(item) == "past its due date"


def test_due_descriptions_read_naturally():
    now = utc("2026-09-01T13:00:00Z")
    soon, tomorrow, later = priority.rank(
        [
            row(id=1, due_at="2026-09-01T16:00:00Z", est_hours_remaining=1.0),
            row(id=2, due_at="2026-09-02T09:00:00Z", est_hours_remaining=1.0),
            row(id=3, due_at="2026-09-20T13:00:00Z", est_hours_remaining=1.0),
        ],
        INDIANA, now,
    )
    assert priority.describe_due(soon, now) == "due in 3h"
    assert priority.describe_due(tomorrow, now) == "due in 20h"
    assert priority.describe_due(later, now).startswith("due ")
