"""Parsing the Canvas calendar feed.

Pure functions only: no network, no database. SPEC §6 calls this feed "the single
load-bearing dependency of the entire project", and every quirk handled here was
observed in Mason's real feed rather than guessed at.

The three that matter, in the order they bite:

1. **Lines are folded.** RFC 5545 wraps anything over about 75 characters and
   continues it on the next line behind a single space. Mason's feed has 330 such
   continuations, and two event titles wrap *inside the course code*::

       SUMMARY:Readiness Assurance Test (RAT) 1: Prepare for limits [FA26-BL-MATH-
        M211-2050]

   Read line by line, that course code is ``FA26-BL-MATH-`` and matches nothing, so
   the event lands in the review queue — quietly, and only for events whose titles
   happen to be long. Unfolding comes before everything else.

2. **Dates arrive in two shapes**, and Canvas repeats a parameter while doing it::

       DTSTART;VALUE=DATE;VALUE=DATE:20260824    (all day, no time at all)
       DTSTART:20260827T131000Z                  (a moment, in UTC)

3. **Text is escaped.** ``Sec 2.8\\, 3.1-3.6`` is one value containing commas, not
   three values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

# A folded line is a line break followed by exactly one space or tab, and the break
# may be CRLF or LF depending on who generated the feed.
_FOLD_RE = re.compile(r"\r?\n[ \t]")

# RFC 5545 escapes only these four things inside a text value.
_ESCAPE_RE = re.compile(r"\\([\\;,nN])")

# 'Title of the thing [FA26-BL-MATH-M211-2050]' -> title, course code.
_SUMMARY_SUFFIX_RE = re.compile(r"^(?P<title>.*?)\s*\[(?P<code>[^\[\]]+)\]\s*$", re.DOTALL)


class IcsError(ValueError):
    """The feed could not be understood."""


@dataclass(frozen=True)
class IcsEvent:
    """One VEVENT, with only the fields this project uses."""

    uid: str
    summary: str
    dtstart: str
    all_day: bool
    tzid: str | None = None
    url: str | None = None

    @property
    def title_and_code(self) -> tuple[str, str | None]:
        return split_summary(self.summary)


def unfold(text: str) -> str:
    """Join continuation lines back onto the line they belong to.

    This must run before the text is split into lines. Everything downstream
    assumes one property per line.
    """
    return _FOLD_RE.sub("", text)


def unescape(value: str) -> str:
    """Undo RFC 5545 text escaping, in a single pass.

    One pass matters: unescaping ``\\\\,`` in two steps would turn a literal
    backslash followed by a comma into a separator.
    """
    return _ESCAPE_RE.sub(lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def split_line(line: str) -> tuple[str, dict[str, list[str]], str]:
    """Split ``NAME;PARAM=VALUE:the value`` into its three parts.

    Parameter values may be quoted and may contain a colon — ``TZID="America/New_York"``
    — so the split happens at the first colon outside quotes rather than the first
    colon. Repeated parameters are kept as a list, because Canvas emits
    ``VALUE=DATE;VALUE=DATE`` and dropping one silently would be worse than keeping
    both.
    """
    in_quotes = False
    for index, char in enumerate(line):
        if char == '"':
            in_quotes = not in_quotes
        elif char == ":" and not in_quotes:
            head, value = line[:index], line[index + 1 :]
            break
    else:
        raise IcsError(f"Property line has no value: {line[:60]!r}")

    parts = _split_params(head)
    name = parts[0].upper()

    params: dict[str, list[str]] = {}
    for part in parts[1:]:
        key, _, raw = part.partition("=")
        params.setdefault(key.upper(), []).append(raw.strip('"'))

    return name, params, value


def _split_params(head: str) -> list[str]:
    """Split on semicolons that are not inside quotes."""
    parts, current, in_quotes = [], [], False
    for char in head:
        if char == '"':
            in_quotes = not in_quotes
            current.append(char)
        elif char == ";" and not in_quotes:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def parse_events(text: str) -> list[IcsEvent]:
    """Read every VEVENT in a calendar.

    Events missing a UID or a DTSTART are skipped rather than raising: one
    malformed entry must not cost Mason the other eleven. SPEC §6 is emphatic that
    a feed problem may never look like an empty schedule.
    """
    if "BEGIN:VCALENDAR" not in text:
        raise IcsError(
            "This does not look like a calendar feed. Canvas may have returned an "
            "error page instead, which happens when the feed address is wrong or "
            "has been reset."
        )

    events: list[IcsEvent] = []
    current: dict[str, tuple[dict[str, list[str]], str]] | None = None

    for line in unfold(text).splitlines():
        line = line.rstrip("\r")
        if not line:
            continue

        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current is not None:
                event = _build_event(current)
                if event is not None:
                    events.append(event)
            current = None
            continue
        if current is None:
            continue

        try:
            name, params, value = split_line(line)
        except IcsError:
            continue
        current[name] = (params, value)

    return events


def _build_event(props: dict[str, tuple[dict[str, list[str]], str]]) -> IcsEvent | None:
    uid = props.get("UID", ({}, ""))[1].strip()
    summary_params, summary = props.get("SUMMARY", ({}, ""))
    dtstart_params, dtstart = props.get("DTSTART", ({}, ""))

    if not uid or not dtstart.strip():
        return None

    tzids = dtstart_params.get("TZID", [])
    return IcsEvent(
        uid=uid,
        summary=unescape(summary).strip(),
        dtstart=dtstart.strip(),
        # Canvas writes VALUE=DATE twice; membership covers one or both.
        all_day="DATE" in dtstart_params.get("VALUE", []),
        tzid=tzids[0] if tzids else None,
        url=props.get("URL", ({}, ""))[1].strip() or None,
    )


def split_summary(summary: str) -> tuple[str, str | None]:
    """Separate an event title from the course code Canvas appends to it.

    SPEC §6.4: there is no course field in the feed, so the bracketed suffix is the
    only association available. A summary without one yields ``None``, and its event
    goes to the review queue rather than being dropped (SPEC §6.5).
    """
    match = _SUMMARY_SUFFIX_RE.match(summary.strip())
    if not match:
        return summary.strip(), None
    return match.group("title").strip(), match.group("code").strip()


def term_code(course_code: str) -> str | None:
    """The term prefix of an SIS course code: FA26-BL-MATH-M211-2050 -> FA26.

    Used to group auto-created courses under a term, since courses cannot exist
    without one. Returns None for a code that does not start with something
    term-shaped, in which case the caller supplies its own grouping.
    """
    head = course_code.split("-", 1)[0].strip().upper()
    return head if re.fullmatch(r"[A-Z]{2}\d{2}", head) else None


def due_at_utc(event: IcsEvent, display_zone: ZoneInfo) -> str:
    """When this event is due, as an ISO 8601 UTC timestamp.

    All-day events carry a date and no time. Canvas means "some time that day", and
    the confirmed rule for this project is 23:59 local — which is what an assignment
    due on a date means to the person it is due from. That local time is then
    converted to UTC, because everything in the database is UTC.
    """
    raw = event.dtstart

    if event.all_day or (len(raw) == 8 and raw.isdigit()):
        try:
            day = date(int(raw[0:4]), int(raw[4:6]), int(raw[6:8]))
        except ValueError as exc:
            raise IcsError(f"Unreadable all-day date {raw!r}") from exc
        local = datetime.combine(day, time(23, 59), tzinfo=display_zone)
        return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    match = re.fullmatch(r"(\d{8})T(\d{6})(Z?)", raw)
    if not match:
        raise IcsError(f"Unreadable start time {raw!r}")

    stamp, clock, zulu = match.groups()
    try:
        naive = datetime(
            int(stamp[0:4]), int(stamp[4:6]), int(stamp[6:8]),
            int(clock[0:2]), int(clock[2:4]), int(clock[4:6]),
        )
    except ValueError as exc:
        raise IcsError(f"Unreadable start time {raw!r}") from exc

    if zulu:
        aware = naive.replace(tzinfo=timezone.utc)
    else:
        # No trailing Z: either the feed named a timezone, or the time is
        # "floating" and means local wherever it is read. Both resolve to a zone
        # here so that nothing is ever stored without one.
        zone = display_zone
        if event.tzid:
            try:
                zone = ZoneInfo(event.tzid)
            except Exception:
                zone = display_zone
        aware = naive.replace(tzinfo=zone)

    return aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def infer_type(title: str) -> str:
    """Guess an assignment type from its title, conservatively.

    Type drives the reminder ladders in SPEC §8, so a sensible guess beats filing
    everything under 'other'. Only unambiguous words count, and the guess is always
    overridable — an exam ladder starting ten days out is noticeable if wrong.
    """
    lowered = title.lower()
    if re.search(r"\bfinal exam\b|\bmidterm\b|\bexam\b", lowered):
        return "exam"
    if re.search(r"\bquiz\b", lowered):
        return "quiz"
    return "other"
