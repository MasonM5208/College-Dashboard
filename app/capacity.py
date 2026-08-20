"""The real capacity model, the timer, and estimate calibration (SPEC §9, M6).

M2 shipped SPEC §9's minimum viable ranking: four productive hours a weekday, flat,
no model. SPEC's instruction was to ship that and *"replace it with measured
reality in October"*. This is the replacement.

**Reconciling the two models SPEC describes.** SPEC §9 asks for both a per-weekday
productive-hours budget *and* for commitments to be subtracted from wall-clock
time. Doing both naively double-counts — an hour of rehearsal would come off the
budget and off the clock. The reading used here::

    available(day) = min(productive_hours[weekday], unbooked_wall_clock(day))
                     − practice_hours_target[weekday]

The budget is what Mason can sustain in a day; the wall clock is what is
physically unbooked; the scarcer of the two governs. A day with eight free hours
and a four-hour budget yields four. A day with two free hours and the same budget
yields two.

**Practice comes off the top, before anything is ranked.** SPEC §9 argues this at
length and it is worth restating: *"Practice has no due date, so in any
deadline-driven ranking it silently loses every comparison — and the owner is a
performance major who will not notice the degradation until roughly a month in."*
Subtracting it here means the priority maths protects practice by default, rather
than practice having to win an argument against a deadline it cannot win.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app import priority

log = logging.getLogger("capacity")

# The window a day's productive hours are treated as spread across. Shared with
# app/priority.py so the two cannot drift apart.
DAY_START = priority.DAY_START
DAY_END = priority.DAY_END

_WINDOW_HOURS = (
    datetime.combine(date(2000, 1, 1), DAY_END)
    - datetime.combine(date(2000, 1, 1), DAY_START)
).total_seconds() / 3600.0

WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday",
                 "Friday", "Saturday", "Sunday")

COMMITMENT_KINDS = ("class", "ensemble", "lesson", "practice", "work", "other")

# SPEC §9's overload trigger needs a window. A week is the unit Mason actually
# plans in, and it is short enough that the shortfall is still actionable — a
# fortnight's shortfall reported today is a fact about October, not a decision.
OVERLOAD_WINDOW_DAYS = 7

# Below this many completed timings, a per-type multiplier is noise dressed as
# data. Three is not statistically respectable; it is the point at which a
# consistent 2x on papers stops looking like one bad afternoon.
CALIBRATION_MIN_SAMPLES = 3


# --- the shape of a week ----------------------------------------------------


@dataclass(frozen=True)
class DayCapacity:
    """One day's budget, with every number that produced it.

    SPEC §9's display rules require the inputs to travel with the answer, so this
    carries them rather than the template recomputing anything.
    """

    day: date
    weekday: int
    productive_hours: float
    practice_hours: float
    committed_hours: float
    unbooked_hours: float
    available_hours: float

    @property
    def weekday_name(self) -> str:
        return WEEKDAY_NAMES[self.weekday]


def settings(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    return {
        row["weekday"]: row
        for row in conn.execute("SELECT * FROM capacity_settings ORDER BY weekday")
    }


def commitments(conn: sqlite3.Connection, active_only: bool = True) -> list[sqlite3.Row]:
    sql = """
        SELECT c.*, co.name AS course_name, co.code AS course_code
          FROM commitments c
          LEFT JOIN courses co ON co.id = c.course_id
    """
    if active_only:
        sql += " WHERE c.active = 1"
    sql += " ORDER BY c.weekday, c.start_time"
    return conn.execute(sql).fetchall()


def _minutes(clock: str) -> int:
    """'HH:MM' as minutes past midnight. Tolerates 'H:MM' and seconds."""
    parts = clock.strip().split(":")
    hours = int(parts[0])
    mins = int(parts[1]) if len(parts) > 1 else 0
    return hours * 60 + mins


def _overlap_hours(rows, weekday: int, window_open: int, window_close: int) -> float:
    """Committed hours on one weekday, inside the working window, merged.

    Merged rather than summed: two commitments that overlap — a lesson inside a
    rehearsal block, or a duplicate entered twice — would otherwise subtract the
    same hour twice and report a day as busier than it is.
    """
    spans = []
    for row in rows:
        if row["weekday"] != weekday:
            continue
        start = max(_minutes(row["start_time"]), window_open)
        end = min(_minutes(row["end_time"]), window_close)
        if end > start:
            spans.append((start, end))

    if not spans:
        return 0.0

    spans.sort()
    merged_total = 0
    current_start, current_end = spans[0]
    for start, end in spans[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            merged_total += current_end - current_start
            current_start, current_end = start, end
    merged_total += current_end - current_start
    return merged_total / 60.0


def day_capacity(
    day: date,
    weekday_settings: dict[int, sqlite3.Row],
    commitment_rows,
    *,
    fraction: float = 1.0,
) -> DayCapacity:
    """One day's available hours.

    `fraction` is the portion of the working window still in play, which is what
    makes today and the deadline's own day count for part of a day rather than all
    of one.
    """
    weekday = day.weekday()
    row = weekday_settings.get(weekday)
    productive = float(row["productive_hours"]) if row else priority.PRODUCTIVE_HOURS_PER_DAY
    practice = float(row["practice_hours_target"]) if row else 0.0

    window_open = _minutes(f"{DAY_START.hour:02d}:{DAY_START.minute:02d}")
    window_close = _minutes(f"{DAY_END.hour:02d}:{DAY_END.minute:02d}")
    committed = _overlap_hours(commitment_rows, weekday, window_open, window_close)

    unbooked = max(_WINDOW_HOURS - committed, 0.0)

    # The scarcer of "what he can sustain" and "what is physically free".
    usable = min(productive, unbooked)

    # Practice comes off the top. Clamped at zero: a practice target larger than
    # the day's budget means no coursework fits, not that time runs backwards.
    available = max(usable - practice, 0.0)

    return DayCapacity(
        day=day,
        weekday=weekday,
        productive_hours=round(productive * fraction, 2),
        practice_hours=round(practice * fraction, 2),
        committed_hours=round(committed * fraction, 2),
        unbooked_hours=round(unbooked * fraction, 2),
        available_hours=round(available * fraction, 2),
    )


def available_hours(
    conn: sqlite3.Connection,
    now: datetime,
    due: datetime,
    zone: ZoneInfo,
) -> float:
    """Productive hours between two moments, under the real model.

    The signature deliberately mirrors `priority.available_hours`, which this
    replaces. A deadline already past yields zero: you cannot have negative time,
    only negative slack.
    """
    if due <= now:
        return 0.0

    weekday_settings = settings(conn)
    commitment_rows = commitments(conn)

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
            total += day_capacity(
                day, weekday_settings, commitment_rows, fraction=fraction
            ).available_hours

        day += timedelta(days=1)

    return round(total, 2)


def week_ahead(
    conn: sqlite3.Connection,
    zone: ZoneInfo,
    now: datetime | None = None,
    days: int = OVERLOAD_WINDOW_DAYS,
) -> list[DayCapacity]:
    """The next `days` days as whole days, for the capacity and review screens."""
    now = now or datetime.now(timezone.utc)
    weekday_settings = settings(conn)
    commitment_rows = commitments(conn)
    start = now.astimezone(zone).date()
    return [
        day_capacity(start + timedelta(days=offset), weekday_settings, commitment_rows)
        for offset in range(days)
    ]


# --- the timer --------------------------------------------------------------


def running(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The timer currently running, if any. At most one — see migration 0008."""
    return conn.execute(
        """
        SELECT t.*, a.title, a.type, a.est_hours_remaining,
               c.name AS course_name
          FROM time_entries t
          JOIN assignments a ON a.id = t.assignment_id
          LEFT JOIN courses c ON c.id = a.course_id
         WHERE t.ended_at IS NULL
         LIMIT 1
        """
    ).fetchone()


class TimerError(RuntimeError):
    """A timer could not be started or stopped, for a reason worth showing."""


def start_timer(conn: sqlite3.Connection, assignment_id: int,
                now: datetime | None = None) -> None:
    """Begin timing work on an assignment.

    Starting a second timer stops the first rather than refusing. Refusing would
    mean noticing the old one is running, going to find it, stopping it, and
    coming back — four steps at the moment attention has already moved on, which
    is how a timer stops being used.
    """
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    existing = running(conn)
    if existing is not None:
        if existing["assignment_id"] == assignment_id:
            return
        stop_timer(conn, now=now)

    row = conn.execute(
        "SELECT 1 FROM assignments WHERE id = ?", (assignment_id,)
    ).fetchone()
    if row is None:
        raise TimerError("That assignment does not exist.")

    conn.execute(
        "INSERT INTO time_entries (assignment_id, started_at) VALUES (?, ?)",
        (assignment_id, stamp),
    )
    conn.execute(
        "UPDATE assignments SET status = 'in_progress' WHERE id = ? "
        "AND status = 'not_started'",
        (assignment_id,),
    )


def stop_timer(conn: sqlite3.Connection, note: str | None = None,
               now: datetime | None = None) -> sqlite3.Row | None:
    """Stop the running timer and book the time against the assignment.

    The logged time is subtracted from `est_hours_remaining`. That is bookkeeping
    rather than the silent inflation SPEC §9 forbids — ninety minutes worked on a
    three-hour task leaves an hour and a half, and the figure is shown on screen
    and editable. What must never happen is an *estimate* changing on its own,
    which is what `calibrate` is careful about.
    """
    now = now or datetime.now(timezone.utc)
    entry = running(conn)
    if entry is None:
        return None

    started = priority._parse(entry["started_at"])
    minutes = 0.0
    if started is not None:
        minutes = max((now - started).total_seconds() / 60.0, 0.0)

    conn.execute(
        "UPDATE time_entries SET ended_at = ?, minutes = ?, note = ? WHERE id = ?",
        (now.strftime("%Y-%m-%dT%H:%M:%SZ"), round(minutes, 2),
         (note or "").strip() or None, entry["id"]),
    )

    remaining = entry["est_hours_remaining"]
    if remaining is not None:
        left = max(float(remaining) - minutes / 60.0, 0.0)
        conn.execute(
            "UPDATE assignments SET est_hours_remaining = ? WHERE id = ?",
            (round(left, 2), entry["assignment_id"]),
        )

    recalibrate(conn)
    return entry


def logged_hours(conn: sqlite3.Connection, assignment_id: int) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(minutes), 0) AS total FROM time_entries "
        "WHERE assignment_id = ? AND ended_at IS NOT NULL",
        (assignment_id,),
    ).fetchone()
    return round(float(row["total"]) / 60.0, 2)


# --- calibration ------------------------------------------------------------


def recalibrate(conn: sqlite3.Connection) -> dict[str, float]:
    """Recompute the per-type multipliers from finished work.

    Only assignments that are *finished* count. A part-timed task in progress has
    logged less than its true cost by definition, and including it would drag every
    multiplier below one — the system would conclude Mason overestimates, which is
    the opposite of what SPEC §9 expects and of what is true.
    """
    rows = conn.execute(
        """
        SELECT a.type,
               a.est_hours,
               (SELECT SUM(t.minutes) / 60.0 FROM time_entries t
                 WHERE t.assignment_id = a.id AND t.ended_at IS NOT NULL) AS actual
          FROM assignments a
         WHERE a.status IN ('submitted','graded')
           AND a.est_hours IS NOT NULL AND a.est_hours > 0
        """
    ).fetchall()

    by_type: dict[str, list[float]] = {}
    for row in rows:
        if row["actual"] is None or row["actual"] <= 0:
            continue
        by_type.setdefault(row["type"], []).append(
            float(row["actual"]) / float(row["est_hours"])
        )

    results = {}
    for assignment_type, ratios in by_type.items():
        ratios.sort()
        middle = len(ratios) // 2
        # Median, not mean. One assignment abandoned half-finished at 3am and
        # timed for six hours should not move the multiplier for every worksheet
        # after it.
        median = (
            ratios[middle]
            if len(ratios) % 2
            else (ratios[middle - 1] + ratios[middle]) / 2
        )
        results[assignment_type] = round(median, 3)
        conn.execute(
            """
            INSERT INTO estimate_calibration (assignment_type, sample_count,
                                              multiplier, updated_at)
            VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            ON CONFLICT(assignment_type) DO UPDATE SET
              sample_count = excluded.sample_count,
              multiplier   = excluded.multiplier,
              updated_at   = excluded.updated_at
            """,
            (assignment_type, len(ratios), max(round(median, 3), 0.01)),
        )
    return results


def calibration(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {
        row["assignment_type"]: row
        for row in conn.execute("SELECT * FROM estimate_calibration ORDER BY assignment_type")
    }


def trusted_multiplier(conn: sqlite3.Connection, assignment_type: str) -> float | None:
    """The multiplier for a type, or None if there is not enough evidence yet.

    Returning None rather than 1.0 is the point: the caller has to decide what to
    say when there is no answer, instead of silently presenting "no data" as
    "you estimate perfectly".
    """
    row = conn.execute(
        "SELECT sample_count, multiplier FROM estimate_calibration "
        "WHERE assignment_type = ?",
        (assignment_type,),
    ).fetchone()
    if row is None or row["sample_count"] < CALIBRATION_MIN_SAMPLES:
        return None
    return float(row["multiplier"])


def describe_calibration(row) -> str:
    """One sentence about a multiplier, in the direction a human thinks in."""
    multiplier = float(row["multiplier"])
    count = int(row["sample_count"])
    noun = "time" if count == 1 else "times"

    if count < CALIBRATION_MIN_SAMPLES:
        return (
            f"timed {count} {noun} — not enough yet to say anything reliable "
            f"({CALIBRATION_MIN_SAMPLES} needed)"
        )
    if multiplier >= 1.15:
        return f"these take about {multiplier:.1f}× your estimate, over {count} timed"
    if multiplier <= 0.85:
        return (
            f"these take about {multiplier:.1f}× your estimate — you allow more "
            f"than you need, over {count} timed"
        )
    return f"your estimates for these are about right, over {count} timed"


# --- overload mode ----------------------------------------------------------
#
# SPEC §9: "The highest-value feature in this specification after reminders
# themselves." Its closing instruction is unusually direct, and is the reason
# nothing below softens anything:
#
#   "Every other academic tracker pretends the owner can do everything and
#    responds to overload by nagging harder. An honest shortfall calculation with
#    a ranked sacrifice list is worth more than the entire reminder system, and it
#    is the calculation the owner would otherwise attempt at 1am under pressure
#    with worse information.
#
#    Do not soften this. Do not hide it behind a toggle. Do not add encouragement."


@dataclass(frozen=True)
class Sacrifice:
    """One candidate to let slide, with what it would actually cost."""

    id: int
    title: str
    course_name: str | None
    type: str
    due_at: str | None
    hours: float
    points: float | None
    grade_cost_pct: float | None
    penalty_pct_per_day: float | None
    reason: str


@dataclass(frozen=True)
class Overload:
    """The state of a window: how much work, how much room, and what to drop."""

    window_days: int
    hours_of_work: float
    hours_available: float
    items: int
    candidates: list[Sacrifice]

    @property
    def shortfall(self) -> float:
        return round(self.hours_of_work - self.hours_available, 2)

    @property
    def overloaded(self) -> bool:
        return self.shortfall > 0

    @property
    def headline(self) -> str:
        """SPEC §9 step 1: "State the shortfall in plain numbers"."""
        return (
            f"{self.hours_of_work:.0f} hours of work, "
            f"{self.hours_available:.0f} hours available"
        )


def _grade_cost(row) -> tuple[float | None, float | None]:
    """What skipping this would cost, as a percentage of the course grade.

    SPEC §9 defines the marginal impact as `points_possible × weight`, discounted
    by `courses.current_grade_pct` — a 25% paper matters more at 79% than at 96%.

    The late penalty is what usually decides it in practice: a professor taking
    nothing for a day makes their work almost free to postpone, and one taking 10%
    a day makes theirs the last thing to touch. Where a penalty is known, the cost
    is a day of that penalty rather than the whole assignment, because letting
    something slide usually means handing it in late rather than never.
    """
    points = row["points_possible"]
    penalty = row["late_penalty_override"]
    if penalty is None:
        penalty = row["penalty_pct_per_day"]

    if penalty is not None:
        # A day late, at this professor's rate, against this assignment's share.
        share = float(points) if points else 0.0
        return (round(float(penalty) * share / 100.0, 2) if share else None,
                float(penalty))

    if points is None:
        return None, None

    # No stated penalty: fall back to the whole value of the work, discounted by
    # how much slack the current grade allows.
    current = row["current_grade_pct"]
    discount = 1.0
    if current is not None:
        # At 96% there is room to lose something; at 79% there is not. Linear
        # between, floored so a high grade never makes a cost look like zero.
        discount = max(min((100.0 - float(current)) / 20.0, 1.0), 0.25)
    return round(float(points) * discount, 2), None


def _sacrifice_reason(row, grade_cost, penalty) -> str:
    if penalty is not None and penalty == 0:
        return "no late penalty in this course — hand it in late for nothing"
    if penalty is not None:
        return f"{penalty:.0f}% per day late in this course"
    if grade_cost is None:
        return "no points recorded, so the cost of dropping it is unknown"
    return f"worth {grade_cost:.0f} points against the course grade"


def overload(
    conn: sqlite3.Connection,
    zone: ZoneInfo,
    now: datetime | None = None,
    days: int = OVERLOAD_WINDOW_DAYS,
) -> Overload:
    """Whether the next `days` days fit, and what is cheapest to drop if not."""
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(days=days)

    rows = conn.execute(
        """
        SELECT a.id, a.title, a.type, a.due_at, a.status,
               a.est_hours_remaining, a.est_hours, a.points_possible,
               a.late_penalty_override,
               c.name AS course_name, c.penalty_pct_per_day, c.current_grade_pct
          FROM assignments a
          LEFT JOIN courses c ON c.id = a.course_id
         WHERE a.status NOT IN ('submitted','graded','dismissed')
           AND a.due_at IS NOT NULL
           AND a.due_at <= ?
        """,
        (horizon.strftime("%Y-%m-%dT%H:%M:%SZ"),),
    ).fetchall()

    hours_of_work = 0.0
    candidates: list[Sacrifice] = []

    for row in rows:
        hours = row["est_hours_remaining"]
        if hours is None:
            hours = row["est_hours"]
        if hours is None:
            # Unestimated work cannot be counted, and saying so is better than
            # quietly assuming zero — which would make an overloaded week look
            # survivable, the one direction this must never be wrong in.
            continue
        hours_of_work += float(hours)

        grade_cost, penalty = _grade_cost(row)
        candidates.append(
            Sacrifice(
                id=int(row["id"]),
                title=row["title"],
                course_name=row["course_name"],
                type=row["type"],
                due_at=row["due_at"],
                hours=round(float(hours), 2),
                points=float(row["points_possible"]) if row["points_possible"] else None,
                grade_cost_pct=grade_cost,
                penalty_pct_per_day=penalty,
                reason=_sacrifice_reason(row, grade_cost, penalty),
            )
        )

    hours_available = sum(
        day.available_hours for day in week_ahead(conn, zone, now=now, days=days)
    )

    # SPEC §9 step 2: cheapest to sacrifice first. An unknown cost sorts last —
    # recommending that something be dropped when there is no idea what it costs
    # is exactly the confident-but-wrong advice that would end the feature.
    #
    # Hours are the tie-break, largest first: between two equally cheap items, the
    # one that frees the most time is the one worth dropping.
    candidates.sort(
        key=lambda item: (
            item.grade_cost_pct is None,
            item.grade_cost_pct if item.grade_cost_pct is not None else 0.0,
            -item.hours,
        )
    )

    return Overload(
        window_days=days,
        hours_of_work=round(hours_of_work, 2),
        hours_available=round(hours_available, 2),
        items=len(candidates),
        candidates=candidates,
    )


def recommended_sacrifices(state: Overload) -> list[Sacrifice]:
    """SPEC §9 step 3: "the specific one or two items to let slide".

    Takes from the cheapest end until the shortfall is covered, and stops there.
    A list of everything that could be dropped is a list nobody acts on; two named
    items with their costs is a decision.
    """
    if not state.overloaded:
        return []

    freed = 0.0
    chosen: list[Sacrifice] = []
    for candidate in state.candidates:
        if freed >= state.shortfall:
            break
        chosen.append(candidate)
        freed += candidate.hours
    return chosen


def ranker(conn: sqlite3.Connection):
    """The capacity-aware `available_fn` for `priority.rank`.

    Settings and commitments are read once and closed over, rather than queried
    per assignment. Ranking twelve items would otherwise mean twenty-four queries
    for data that cannot change during a single page render.
    """
    weekday_settings = settings(conn)
    commitment_rows = commitments(conn)

    def available(now: datetime, due: datetime, zone: ZoneInfo) -> float:
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
                total += day_capacity(
                    day, weekday_settings, commitment_rows, fraction=fraction
                ).available_hours

            day += timedelta(days=1)

        return round(total, 2)

    return available
