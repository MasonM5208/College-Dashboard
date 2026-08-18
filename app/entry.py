"""Turning things Mason types into assignments.

Canvas only carries work that has a due date set in it, which for a performance
major leaves most of the semester invisible (SPEC §6.3). Everything else is typed
in, so this module is what makes the dashboard cover the whole picture rather than
the fraction Canvas happens to know about.

Pure functions, no database. Dates and durations are parsed here so the parsing can
be tested directly rather than through a form.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app import ics

TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"

# SPEC §9: "Prompt for it on every create; default from type." Unlike ingestion,
# where a silent guess would go unseen, these appear in a form with the number
# visible and editable before anything is saved.
#
# Papers and projects are deliberately generous. SPEC §9 notes the owner will
# underestimate papers by roughly 2x at first, as everyone does, and M6 replaces
# these constants with a multiplier measured from his own logged time.
DEFAULT_HOURS_BY_TYPE = {
    "worksheet": 1.5,
    "quiz": 1.0,
    "exam": 4.0,
    "paper": 6.0,
    "project": 8.0,
    "performance": 2.0,
    "milestone": 2.0,
    "other": 1.0,
}

ASSIGNMENT_TYPES = tuple(DEFAULT_HOURS_BY_TYPE)

# When a date is given with no time. Matches the rule used for all-day Canvas
# events, so a deadline means the same thing wherever it came from.
DEFAULT_DUE_TIME = time(23, 59)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Tried in order, and each is anchored, because a single combined pattern reads
# "90m" as 9 hours and 0 minutes: the hours unit is optional, so the digits are
# taken greedily before the minutes part ever gets a look.
_HOURS_UNIT = r"h|hr|hrs|hour|hours"
_MINUTES_UNIT = r"m|min|mins|minute|minutes"

_DURATION_PATTERNS = (
    # "1h30m" — both parts, units required on each.
    re.compile(rf"^(?P<hours>\d+(?:\.\d+)?)\s*(?:{_HOURS_UNIT})"
               rf"\s*(?P<minutes>\d+)\s*(?:{_MINUTES_UNIT})$", re.IGNORECASE),
    # "90m" — minutes alone, unit required so it cannot be read as hours.
    re.compile(rf"^(?P<minutes>\d+)\s*(?:{_MINUTES_UNIT})$", re.IGNORECASE),
    # "2h" or a bare "2", which means hours.
    re.compile(rf"^(?P<hours>\d+(?:\.\d+)?)\s*(?:{_HOURS_UNIT})?$", re.IGNORECASE),
)

_TIME_RE = re.compile(
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm)?\s*$",
    re.IGNORECASE,
)


class EntryError(ValueError):
    """Something Mason typed could not be understood. The message is shown to him."""


def default_hours(assignment_type: str) -> float:
    return DEFAULT_HOURS_BY_TYPE.get(assignment_type, DEFAULT_HOURS_BY_TYPE["other"])


def parse_hours(text: str | None) -> float | None:
    """Read '2h', '90m', '1.5', '1h30m' as a number of hours."""
    if text is None or not text.strip():
        return None

    cleaned = " ".join(text.split())
    for pattern in _DURATION_PATTERNS:
        match = pattern.match(cleaned)
        if match:
            break
    else:
        raise EntryError(f"{cleaned!r} is not a length of time. Try 2h, 90m or 1.5.")

    groups = match.groupdict()
    hours = float(groups.get("hours") or 0)
    hours += float(groups.get("minutes") or 0) / 60.0

    if hours <= 0:
        raise EntryError("A length of time has to be more than zero.")
    if hours > 100:
        raise EntryError("That is more than 100 hours. Break it into smaller pieces.")
    return round(hours, 2)


def _split_date_and_time(text: str) -> tuple[str, time | None]:
    """Separate a trailing clock time from a date."""
    parts = text.strip().split()
    if len(parts) < 2:
        return text.strip(), None

    match = _TIME_RE.match(parts[-1])
    if not match:
        return text.strip(), None

    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    meridiem = (match.group("meridiem") or "").lower()

    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    elif not meridiem and match.group("minute") is None:
        # A bare number with no colon and no am/pm is not a time.
        return text.strip(), None

    if hour > 23 or minute > 59:
        raise EntryError(f"{parts[-1]!r} is not a time of day.")

    return " ".join(parts[:-1]), time(hour, minute)


def _parse_date(text: str, today: date) -> date:
    """Read a date in any of the shapes a person actually types.

    A date with no year is taken as the next one to occur, so typing a syllabus in
    August does not file December's work under last December.
    """
    cleaned = text.strip().replace(",", " ")
    cleaned = re.sub(r"\s+", " ", cleaned)

    iso = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", cleaned)
    if iso:
        year, month, day = (int(part) for part in iso.groups())
        return _make_date(year, month, day, cleaned)

    slashes = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?", cleaned)
    if slashes:
        month, day, year_text = int(slashes.group(1)), int(slashes.group(2)), slashes.group(3)
        if year_text is None:
            return _next_occurrence(month, day, today, cleaned)
        year = int(year_text)
        if year < 100:
            year += 2000
        return _make_date(year, month, day, cleaned)

    words = re.fullmatch(
        r"(?:([A-Za-z]{3,9})\s+(\d{1,2})|(\d{1,2})\s+([A-Za-z]{3,9}))(?:\s+(\d{4}))?",
        cleaned,
    )
    if words:
        name = (words.group(1) or words.group(4)).lower()[:3]
        day = int(words.group(2) or words.group(3))
        if name not in _MONTHS:
            raise EntryError(f"{cleaned!r} is not a date this understands.")
        month = _MONTHS[name]
        if words.group(5):
            return _make_date(int(words.group(5)), month, day, cleaned)
        return _next_occurrence(month, day, today, cleaned)

    raise EntryError(
        f"{cleaned!r} is not a date. Try 2026-09-08, 9/8, or Sep 8."
    )


def _make_date(year: int, month: int, day: int, original: str) -> date:
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise EntryError(f"{original!r} is not a real date.") from exc


def _next_occurrence(month: int, day: int, today: date, original: str) -> date:
    for year in (today.year, today.year + 1):
        candidate = _make_date(year, month, day, original)
        if candidate >= today:
            return candidate
    return _make_date(today.year, month, day, original)


def parse_when(text: str | None, zone: ZoneInfo, now: datetime | None = None) -> str | None:
    """Read a due date, with an optional time, as an ISO 8601 UTC timestamp."""
    if text is None or not text.strip():
        return None

    now = now or datetime.now(timezone.utc)
    date_part, clock = _split_date_and_time(text)
    day = _parse_date(date_part, now.astimezone(zone).date())
    local = datetime.combine(day, clock or DEFAULT_DUE_TIME, tzinfo=zone)
    return local.astimezone(timezone.utc).strftime(TIMESTAMP_FMT)


def start_by_for(due_at: str | None, est_hours: float | None, assignment_type: str) -> str | None:
    """When work on this ought to begin.

    SPEC §5: `due_at - (est_hours × 2 days)` for papers and projects, nothing for
    anything else. A six-hour paper therefore wants starting twelve days out, which
    is the point of having the field at all — long work loses to short work in every
    deadline-ordered list until something says when to begin.
    """
    if assignment_type not in ("paper", "project"):
        return None
    if not due_at or not est_hours:
        return None

    due = datetime.strptime(due_at, TIMESTAMP_FMT).replace(tzinfo=timezone.utc)
    return (due - timedelta(days=est_hours * 2)).strftime(TIMESTAMP_FMT)


@dataclass
class ParsedLine:
    """One line of a pasted syllabus, understood or not."""

    number: int
    raw: str
    title: str = ""
    due_at: str | None = None
    est_hours: float | None = None
    type: str = "other"
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def parse_batch(
    text: str,
    zone: ZoneInfo,
    now: datetime | None = None,
) -> list[ParsedLine]:
    """Read a pasted syllabus, one assignment per line.

        Species counterpoint 1 | 2026-09-08 | 2h
        Listening journal wk3  | Sep 17
        Midterm exam           | 10/6 | 6h | exam

    Title is required; date, length and type are optional and positional. A line
    that cannot be read comes back carrying its own error rather than being
    dropped, so the preview can show exactly what was not understood and nothing
    disappears silently.
    """
    now = now or datetime.now(timezone.utc)
    results: list[ParsedLine] = []

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        parsed = ParsedLine(number=number, raw=line)
        fields = [part.strip() for part in line.split("|")]

        parsed.title = fields[0]
        if not parsed.title:
            parsed.error = "No title on this line."
            results.append(parsed)
            continue
        if len(parsed.title) > 300:
            parsed.title = parsed.title[:300]

        try:
            if len(fields) > 1 and fields[1]:
                parsed.due_at = parse_when(fields[1], zone, now)
            if len(fields) > 2 and fields[2]:
                parsed.est_hours = parse_hours(fields[2])
            if len(fields) > 3 and fields[3]:
                given = fields[3].lower()
                if given not in ASSIGNMENT_TYPES:
                    raise EntryError(
                        f"{fields[3]!r} is not a type. Use one of: "
                        + ", ".join(ASSIGNMENT_TYPES)
                    )
                parsed.type = given
            else:
                parsed.type = ics.infer_type(parsed.title)
        except EntryError as exc:
            parsed.error = str(exc)

        results.append(parsed)

    return results
