"""Ranking work by slack.

SPEC §9 opens by rejecting the obvious approach: *"Due-date ordering must not be
the default sort. It ranks a 20-minute worksheet due tomorrow above a 6-hour paper
due Thursday, which is backwards and will eventually cost a grade."*

The primitive::

    slack_hours = available_hours_before(due_at) − est_hours_remaining

Sorted ascending. Negative slack means the work already does not fit in the time
left, which shows up days before a due-date sort notices anything is wrong.

`available_hours_before` is **not** wall-clock time remaining. It is productive
hours. M2 ships the minimum viable version SPEC §9 describes — a flat constant,
no capacity model — and M6 replaces the constant with measured reality from
`commitments` and `capacity_settings`.

Everything here is a pure function of its arguments so the arithmetic can be
tested directly, which matters more than usual: SPEC §9's display rules say the
first time this is visibly wrong is the last time it gets trusted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

# SPEC §9: "a flat constant of 4 productive hours per weekday". Confirmed to apply
# to every day including weekends — counting weekends as zero would report anything
# due Monday as already lost by Friday lunchtime, and a ranking that panics wrongly
# is one that stops being read.
PRODUCTIVE_HOURS_PER_DAY = 4.0

# Those hours are treated as spread across a waking window rather than landing at
# any particular hour. The window sits inside SPEC §8's quiet hours of 22:30–07:30,
# so the two models do not contradict each other.
DAY_START = time(8, 0)
DAY_END = time(22, 0)

_WINDOW_HOURS = (
    datetime.combine(date(2000, 1, 1), DAY_END)
    - datetime.combine(date(2000, 1, 1), DAY_START)
).total_seconds() / 3600.0

# SPEC §9: a small nudge so that something already open stays near the top and does
# not lose to endless context switching. "Small. Reduces context-switch churn; must
# never dominate slack."
IN_PROGRESS_BONUS_HOURS = 0.5

# Statuses that are finished and drop out of the ranking entirely.
DONE_STATUSES = frozenset({"submitted", "graded", "dismissed"})


@dataclass(frozen=True)
class Ranked:
    """One assignment, with every number that decided its position.

    SPEC §9's display rules forbid showing a bare score, and require that the
    inputs travel with the item — so they are fields here rather than something the
    template recomputes.
    """

    id: int
    title: str
    course_name: str | None
    course_code: str | None
    type: str
    status: str
    due_at: str | None
    due_local: datetime | None
    hours_left: float | None
    hours_free: float | None
    slack: float | None
    pinned: bool
    overdue: bool
    needs_estimate: bool

    @property
    def rankable(self) -> bool:
        return self.slack is not None


def available_hours(now: datetime, due: datetime, zone: ZoneInfo) -> float:
    """Productive hours between two moments.

    Counts the overlap of each local day's working window with the interval, and
    scales it to the daily budget. A deadline already past yields zero rather than
    a negative number: you cannot have negative time, only negative slack.
    """
    if due <= now:
        return 0.0

    local_now = now.astimezone(zone)
    local_due = due.astimezone(zone)

    total = 0.0
    day = local_now.date()
    while day <= local_due.date():
        window_open = datetime.combine(day, DAY_START, tzinfo=zone)
        window_close = datetime.combine(day, DAY_END, tzinfo=zone)

        start = max(window_open, local_now)
        end = min(window_close, local_due)

        if end > start:
            fraction = (end - start).total_seconds() / 3600.0 / _WINDOW_HOURS
            total += fraction * PRODUCTIVE_HOURS_PER_DAY

        day += timedelta(days=1)

    return round(total, 2)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def rank(rows, zone: ZoneInfo, now: datetime | None = None) -> list[Ranked]:
    """Order assignments by how little room is left, least first.

    Ordering, in the order the tie-breaks apply:

    1. Pinned items, which SPEC §9 says always win.
    2. Ascending slack — least room first.
    3. Earliest due date, for genuine ties.

    Items with no estimate, or no due date, cannot be placed. They come back with
    `slack is None` so the caller can ask for what is missing rather than guessing
    at it, which SPEC §9 forbids.
    """
    now = now or datetime.now(timezone.utc)
    ranked: list[Ranked] = []

    for row in rows:
        status = row["status"]
        if status in DONE_STATUSES:
            continue

        due = _parse(row["due_at"])
        hours_left = row["est_hours_remaining"]
        if hours_left is None:
            hours_left = row["est_hours"]

        hours_free = available_hours(now, due, zone) if due else None

        slack = None
        if due is not None and hours_left is not None:
            # The in-progress nudge is applied in the sort key alone, never here:
            # a displayed "hours free" that had been quietly adjusted would be a
            # number Mason did not type and cannot check.
            slack = round(hours_free - float(hours_left), 2)

        ranked.append(
            Ranked(
                id=int(row["id"]),
                title=row["title"],
                course_name=row["course_name"],
                course_code=row["course_code"],
                type=row["type"],
                status=status,
                due_at=row["due_at"],
                due_local=due.astimezone(zone) if due else None,
                hours_left=float(hours_left) if hours_left is not None else None,
                hours_free=hours_free,
                slack=slack,
                pinned=bool(row["pinned"]),
                overdue=bool(due and due <= now),
                needs_estimate=hours_left is None,
            )
        )

    def sort_key(item: Ranked):
        effective = item.slack
        if effective is not None and item.status == "in_progress":
            effective -= IN_PROGRESS_BONUS_HOURS
        return (
            not item.pinned,
            effective is None,
            effective if effective is not None else 0.0,
            item.due_at or "9999",
        )

    return sorted(ranked, key=sort_key)


def describe_slack(item: Ranked) -> str:
    """The one sentence SPEC §9 requires the sort to be explainable in."""
    if item.slack is None:
        return "needs a time estimate before it can be ranked"
    if item.overdue:
        return "past its due date"
    if item.slack < 0:
        return f"{abs(item.slack):.1f}h short of the time needed"
    if item.slack < 2:
        return f"only {item.slack:.1f}h to spare"
    return f"{item.slack:.1f}h to spare"


def describe_due(item: Ranked, now: datetime | None = None) -> str:
    """A relative description of the deadline, for reading at a glance."""
    if item.due_local is None:
        return "no due date"

    now = now or datetime.now(timezone.utc)
    delta = item.due_local - now.astimezone(item.due_local.tzinfo)
    hours = delta.total_seconds() / 3600.0

    if hours < 0:
        days = int(abs(hours) // 24)
        return "overdue" if days == 0 else f"{days}d overdue"
    if hours < 1:
        return f"due in {int(delta.total_seconds() // 60)} min"
    if hours < 24:
        return f"due in {int(hours)}h"
    return f"due {item.due_local:%a %-d %b}"
