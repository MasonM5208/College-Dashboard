"""Tests for the Canvas feed parser.

Every case here comes from a quirk observed in the real feed. The fixture at
tests/fixtures/canvas_sample.ics reproduces the same structure with invented
titles and course codes.
"""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import ics

FIXTURE = Path(__file__).parent / "fixtures" / "canvas_sample.ics"
INDIANA = ZoneInfo("America/Indiana/Indianapolis")


@pytest.fixture
def feed() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture
def events(feed) -> list[ics.IcsEvent]:
    return ics.parse_events(feed)


# --- folding ----------------------------------------------------------------


def test_unfolding_rejoins_a_continued_line():
    text = "SUMMARY:Something long [FA26-XX-ANTH-\n A100-11111]\r\nUID:x\r\n"
    assert "[FA26-XX-ANTH-A100-11111]" in ics.unfold(text)


def test_unfolding_accepts_both_line_ending_styles():
    assert ics.unfold("A:one\r\n two") == "A:onetwo"
    assert ics.unfold("A:one\n two") == "A:onetwo"


def test_a_course_code_split_across_lines_is_still_matched(events):
    """The failure this parser exists to avoid.

    Read line by line, this event's code is 'FA26-X' and matches nothing, so it
    would land in the review queue — silently, and only for long titles.
    """
    event = next(e for e in events if e.uid.endswith("1000002"))
    title, code = event.title_and_code
    assert code == "FA26-XX-ANTH-A100-11111"
    assert title == "Diagnostic assessment covering everything from the first week"


def test_an_ordinary_line_starting_with_a_space_is_not_invented():
    """Only a line break followed by whitespace folds; spaces elsewhere stay put."""
    assert ics.unfold("SUMMARY:a b c") == "SUMMARY:a b c"


# --- escaping ---------------------------------------------------------------


def test_escaped_commas_are_restored(events):
    event = next(e for e in events if e.uid.endswith("1000004"))
    title, _ = event.title_and_code
    assert title == "Exam 2 Attempt 2 Sec 2.8, 3.1-3.6, 3.9 (optional)"


def test_escape_sequences_are_undone_in_one_pass():
    # A literal backslash followed by a comma, not an escaped comma.
    assert ics.unescape(r"a\\,b") == r"a\,b"
    assert ics.unescape(r"x\;y") == "x;y"
    assert ics.unescape(r"line\nbreak") == "line\nbreak"
    assert ics.unescape(r"line\Nbreak") == "line\nbreak"


# --- property lines ---------------------------------------------------------


def test_repeated_parameters_are_all_kept():
    """Canvas emits VALUE=DATE twice on the same property."""
    name, params, value = ics.split_line("DTSTART;VALUE=DATE;VALUE=DATE:20260824")
    assert name == "DTSTART"
    assert params["VALUE"] == ["DATE", "DATE"]
    assert value == "20260824"


def test_a_quoted_parameter_may_contain_a_colon():
    name, params, value = ics.split_line(
        'DTSTART;TZID="America/New_York":20260903T090000'
    )
    assert params["TZID"] == ["America/New_York"]
    assert value == "20260903T090000"


def test_a_line_with_no_value_is_rejected():
    with pytest.raises(ics.IcsError):
        ics.split_line("BROKEN LINE")


# --- events -----------------------------------------------------------------


def test_every_well_formed_event_is_parsed(events):
    # Eight VEVENTs in the fixture, one of which has no UID.
    assert len(events) == 7


def test_an_event_without_a_uid_is_skipped_not_fatal(events):
    """One malformed entry must not cost the others (SPEC §6)."""
    assert all(e.uid for e in events)
    assert not any("no UID" in e.summary for e in events)


def test_something_that_is_not_a_calendar_is_refused():
    """Canvas serves an HTML error page when the feed address is wrong."""
    with pytest.raises(ics.IcsError, match="does not look like a calendar feed"):
        ics.parse_events("<!DOCTYPE html><html><body>Not authorized</body></html>")


def test_a_truncated_feed_yields_what_it_can():
    text = FIXTURE.read_text(encoding="utf-8")
    cut = text[: text.index("UID:event-assignment-1000004")]
    parsed = ics.parse_events(cut)
    assert len(parsed) == 3
    assert parsed[0].uid.endswith("1000001")


# --- course association -----------------------------------------------------


def test_the_bracketed_suffix_becomes_the_course_code():
    title, code = ics.split_summary("Reading response one [FA26-XX-ANTH-A100-11111]")
    assert title == "Reading response one"
    assert code == "FA26-XX-ANTH-A100-11111"


def test_a_summary_with_no_suffix_has_no_course(events):
    """SPEC §6.5: this goes to the review queue, never silently dropped."""
    event = next(e for e in events if e.uid.endswith("1000006"))
    title, code = event.title_and_code
    assert title == "Departmental recital attendance"
    assert code is None


def test_a_title_containing_brackets_keeps_them():
    title, code = ics.split_summary("Read [pages 40-50] closely [FA26-XX-ANTH-A100-1]")
    assert title == "Read [pages 40-50] closely"
    assert code == "FA26-XX-ANTH-A100-1"


def test_the_term_prefix_is_extracted():
    assert ics.term_code("FA26-BL-MATH-M211-2050") == "FA26"
    assert ics.term_code("SP27-BL-MUS-P100-1") == "SP27"


def test_a_code_with_no_term_prefix_returns_none():
    assert ics.term_code("SOMETHING-ELSE") is None
    assert ics.term_code("") is None


# --- times ------------------------------------------------------------------


def test_an_all_day_event_is_due_at_the_end_of_that_day(events):
    """The confirmed rule: a bare date means 23:59 local, stored as UTC."""
    event = next(e for e in events if e.uid.endswith("1000001"))
    assert event.all_day is True
    # 23:59 on 24 Aug 2026 in Indiana (EDT, UTC-4) is 03:59 UTC the next day.
    assert ics.due_at_utc(event, INDIANA) == "2026-08-25T03:59:00Z"


def test_a_utc_timestamp_is_kept_as_it_is(events):
    event = next(e for e in events if e.uid.endswith("1000003"))
    assert event.all_day is False
    assert ics.due_at_utc(event, INDIANA) == "2026-09-16T13:10:00Z"


def test_the_duplicated_value_date_parameter_still_reads_as_all_day(events):
    event = next(e for e in events if e.uid.endswith("1000001"))
    assert event.all_day is True


def test_a_named_timezone_is_honoured(events):
    event = next(e for e in events if e.uid.endswith("1000007"))
    # 09:00 Indiana time in September (EDT, UTC-4) is 13:00 UTC.
    assert ics.due_at_utc(event, INDIANA) == "2026-09-03T13:00:00Z"


def test_all_day_conversion_follows_daylight_saving():
    """Indiana is UTC-4 in summer and UTC-5 in winter; 23:59 stays 23:59 locally."""
    summer = ics.IcsEvent(uid="a", summary="s", dtstart="20260701", all_day=True)
    winter = ics.IcsEvent(uid="b", summary="s", dtstart="20261201", all_day=True)
    assert ics.due_at_utc(summer, INDIANA) == "2026-07-02T03:59:00Z"
    assert ics.due_at_utc(winter, INDIANA) == "2026-12-02T04:59:00Z"


def test_an_unreadable_date_is_reported():
    event = ics.IcsEvent(uid="a", summary="s", dtstart="not-a-date", all_day=False)
    with pytest.raises(ics.IcsError):
        ics.due_at_utc(event, INDIANA)


# --- type inference ---------------------------------------------------------


def test_exams_and_quizzes_are_recognised():
    assert ics.infer_type("Exam 1 Attempt 1 Sec 1.1 - 2.7") == "exam"
    assert ics.infer_type("Midterm review session") == "exam"
    assert ics.infer_type("Quiz on lab safety") == "quiz"


def test_anything_ambiguous_stays_other():
    """A wrong guess drives a wrong reminder ladder, so guess only when sure."""
    assert ics.infer_type("Chapter 2 Homework") == "other"
    assert ics.infer_type("Reading response one") == "other"
    assert ics.infer_type("Examine the specimen closely") == "other"
