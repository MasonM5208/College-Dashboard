"""Tests for parsing the things Mason types (manual and syllabus batch entry)."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app import entry

INDIANA = ZoneInfo("America/Indiana/Indianapolis")
NOW = datetime(2026, 8, 17, 13, 0, tzinfo=timezone.utc)  # Mon 17 Aug, 09:00 local


# --- durations --------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2h", 2.0), ("2", 2.0), ("1.5h", 1.5), ("90m", 1.5),
        ("30min", 0.5), ("45 minutes", 0.75), ("1h30m", 1.5), (" 3 hours ", 3.0),
    ],
)
def test_durations_are_read_the_way_people_write_them(text, expected):
    assert entry.parse_hours(text) == pytest.approx(expected)


def test_no_duration_is_not_an_error():
    """Leaving the length off is allowed; the item joins the estimate prompts."""
    assert entry.parse_hours("") is None
    assert entry.parse_hours(None) is None


@pytest.mark.parametrize("text", ["soon", "ages", "-2h", "0", "200h"])
def test_nonsense_durations_are_refused_with_a_reason(text):
    with pytest.raises(entry.EntryError):
        entry.parse_hours(text)


# --- dates ------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2026-09-08", "2026-09-09T03:59:00Z"),   # 23:59 local, EDT is UTC-4
        ("9/8", "2026-09-09T03:59:00Z"),
        ("9/8/2026", "2026-09-09T03:59:00Z"),
        ("9/8/26", "2026-09-09T03:59:00Z"),
        ("Sep 8", "2026-09-09T03:59:00Z"),
        ("September 8", "2026-09-09T03:59:00Z"),
        ("8 Sep", "2026-09-09T03:59:00Z"),
        ("Sep 8, 2026", "2026-09-09T03:59:00Z"),
    ],
)
def test_dates_are_read_in_the_shapes_people_type(text, expected):
    assert entry.parse_when(text, INDIANA, NOW) == expected


def test_a_date_with_no_time_means_the_end_of_that_day():
    """Same rule as Canvas all-day events, so a deadline means one thing."""
    assert entry.parse_when("2026-09-08", INDIANA, NOW) == "2026-09-09T03:59:00Z"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2026-09-08 14:30", "2026-09-08T18:30:00Z"),
        ("2026-09-08 2:30pm", "2026-09-08T18:30:00Z"),
        ("9/8 9am", "2026-09-08T13:00:00Z"),
        ("Sep 8 11:59pm", "2026-09-09T03:59:00Z"),
        ("Sep 8 12am", "2026-09-08T04:00:00Z"),
    ],
)
def test_a_time_of_day_is_honoured(text, expected):
    assert entry.parse_when(text, INDIANA, NOW) == expected


def test_a_date_with_no_year_means_the_next_one_to_come():
    """Typing a syllabus in August must not file December under last December."""
    december = entry.parse_when("12/15", INDIANA, NOW)
    assert december.startswith("2026-12-16")

    # A date already past this year belongs to next year.
    january = entry.parse_when("1/20", INDIANA, NOW)
    assert january.startswith("2027-01-21")


def test_winter_dates_use_winter_time():
    """Indiana is UTC-5 in December, so 23:59 local is 04:59 UTC, not 03:59."""
    assert entry.parse_when("2026-12-15", INDIANA, NOW) == "2026-12-16T04:59:00Z"


@pytest.mark.parametrize("text", ["next tuesday", "2026-13-01", "2/30", "Smarch 4", "later"])
def test_unreadable_dates_are_refused_with_a_reason(text):
    with pytest.raises(entry.EntryError):
        entry.parse_when(text, INDIANA, NOW)


def test_no_date_is_allowed():
    assert entry.parse_when("", INDIANA, NOW) is None
    assert entry.parse_when(None, INDIANA, NOW) is None


# --- defaults from type -----------------------------------------------------


def test_each_type_has_a_starting_estimate():
    """SPEC §9: "Prompt for it on every create; default from type.\""""
    assert entry.default_hours("paper") == 6.0
    assert entry.default_hours("quiz") == 1.0
    assert entry.default_hours("exam") == 4.0
    assert entry.default_hours("anything else") == entry.default_hours("other")


def test_papers_and_projects_get_a_start_date(  ):
    """SPEC §5: due_at − (est_hours × 2 days), so long work does not lose to short."""
    start = entry.start_by_for("2026-09-20T03:59:00Z", 6.0, "paper")
    assert start == "2026-09-08T03:59:00Z"  # twelve days earlier


def test_other_types_have_no_start_date():
    assert entry.start_by_for("2026-09-20T03:59:00Z", 1.0, "worksheet") is None
    assert entry.start_by_for("2026-09-20T03:59:00Z", None, "paper") is None
    assert entry.start_by_for(None, 6.0, "paper") is None


# --- pasted syllabus --------------------------------------------------------


SYLLABUS = """
Species counterpoint 1 | 2026-09-08 | 2h
Species counterpoint 2 | 9/15 | 2h
Listening journal wk3  | Sep 17
Midterm exam           | 10/6 | 6h
Final portfolio        | 12/10 | 10h | project
"""


def test_a_pasted_syllabus_becomes_assignments():
    lines = entry.parse_batch(SYLLABUS, INDIANA, NOW)
    assert len(lines) == 5
    assert all(line.ok for line in lines)
    assert [line.title for line in lines][:2] == [
        "Species counterpoint 1", "Species counterpoint 2"
    ]


def test_the_length_is_optional_on_a_line():
    lines = entry.parse_batch(SYLLABUS, INDIANA, NOW)
    journal = next(line for line in lines if "journal" in line.title)
    assert journal.est_hours is None
    assert journal.due_at is not None


def test_types_are_inferred_or_stated():
    lines = entry.parse_batch(SYLLABUS, INDIANA, NOW)
    by_title = {line.title: line.type for line in lines}
    assert by_title["Midterm exam"] == "exam"          # inferred
    assert by_title["Final portfolio"] == "project"    # stated
    assert by_title["Species counterpoint 1"] == "other"


def test_blank_lines_and_comments_are_skipped():
    lines = entry.parse_batch(
        "\n\n# from the syllabus PDF\nReal item | 9/8\n\n", INDIANA, NOW
    )
    assert len(lines) == 1
    assert lines[0].title == "Real item"


def test_a_bad_line_carries_its_error_and_is_not_dropped():
    """Nothing disappears silently — the preview has to be able to show why."""
    lines = entry.parse_batch(
        "Good one | 9/8 | 1h\nBad one | someday | 1h\nAlso good | 9/9", INDIANA, NOW
    )
    assert len(lines) == 3
    assert [line.ok for line in lines] == [True, False, True]
    assert "not a date" in lines[1].error
    assert lines[1].raw == "Bad one | someday | 1h"


def test_a_line_with_only_a_title_is_fine():
    lines = entry.parse_batch("Read chapter 4", INDIANA, NOW)
    assert lines[0].ok
    assert lines[0].due_at is None
    assert lines[0].est_hours is None


def test_a_line_with_no_title_is_refused():
    lines = entry.parse_batch(" | 9/8 | 1h", INDIANA, NOW)
    assert not lines[0].ok
    assert "No title" in lines[0].error


def test_an_unknown_type_is_refused_with_the_list():
    lines = entry.parse_batch("Thing | 9/8 | 1h | essay", INDIANA, NOW)
    assert not lines[0].ok
    assert "worksheet" in lines[0].error


def test_line_numbers_survive_for_pointing_at_the_mistake():
    lines = entry.parse_batch("\nfirst | 9/8\nbroken | nope\n", INDIANA, NOW)
    assert lines[1].number == 3
