"""Working out when to nudge, and writing those moments down.

SPEC §8 sets the ladders and the quiet hours. SPEC §5 sets the storage rule:

    "Materialize every reminder as its own row. This is what makes individual
    snoozing possible and keeps state coherent when a due date moves. When a due
    date changes, mark affected pending instances `superseded` and generate new
    ones — do not mutate `fire_at` in place, so the history stays auditable."

Everything here is arithmetic over dates, with no network and no Apple in sight,
so it can be tested directly. That matters more than usual: a reminder that fires
at the wrong hour is worse than one that never fires, because it teaches Mason to
ignore the ones that are right.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger("reminders")

TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"

# SPEC §8: "Quiet hours 22:30–07:30. Anything landing inside shifts to the nearest
# edge — earlier for due_by, later for start_by."
QUIET_START = time(22, 30)
QUIET_END = time(7, 30)

# Named rungs that are a time of day rather than an offset.
MORNING_OF = "MORNING_OF"      # 08:00 local on the due date
NIGHT_BEFORE = "NIGHT_BEFORE"  # 21:00 local the evening before

MORNING_OF_AT = time(8, 0)
NIGHT_BEFORE_AT = time(21, 0)

# Instances that are still live. A due date moving supersedes all of these; an
# assignment with none of them needs its ladder built.
LIVE_STATES = ("pending", "sent", "snoozed")

# Work that is finished has nothing left to nudge about.
DONE_STATUSES = ("submitted", "graded", "dismissed")

_DURATION_RE = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")


@dataclass(frozen=True)
class Rung:
    """One step of a ladder: when to nudge, and whether it is a start or a due."""

    fire_at: datetime
    kind: str  # 'start_by' or 'due_by'


def parse_offset(text: str) -> timedelta | None:
    """Read 'P7D' or 'PT3H' as a length of time before the deadline.

    Returns None for the named rungs, which are a clock time rather than an
    offset and are handled separately.
    """
    if text in (MORNING_OF, NIGHT_BEFORE):
        return None

    match = _DURATION_RE.match(text)
    if not match or not any(match.groups()):
        raise ValueError(f"{text!r} is not a duration this understands")

    days, hours, minutes = (int(part) if part else 0 for part in match.groups())
    return timedelta(days=days, hours=hours, minutes=minutes)


def in_quiet_hours(moment: datetime) -> bool:
    """Whether a local time falls in the 22:30–07:30 window.

    The window wraps midnight, so it is two comparisons rather than one.
    """
    clock = moment.timetz().replace(tzinfo=None)
    return clock >= QUIET_START or clock < QUIET_END


def shift_out_of_quiet_hours(moment: datetime, kind: str) -> datetime:
    """Move a nudge to the nearest edge of the quiet window (SPEC §8).

    A `due_by` moves **earlier** — a deadline warning is useless after the fact.
    A `start_by` moves **later**, since starting work is a morning activity.

    The case worth naming: "earlier" for something landing at 03:00 means 22:30
    *the previous evening*, not 22:30 the same day, which is still ahead of it.
    """
    if not in_quiet_hours(moment):
        return moment

    if kind == "due_by":
        if moment.timetz().replace(tzinfo=None) < QUIET_END:
            # Small hours: the nearest earlier edge is last night.
            evening = (moment - timedelta(days=1)).replace(
                hour=QUIET_START.hour, minute=QUIET_START.minute, second=0, microsecond=0
            )
            return evening
        return moment.replace(
            hour=QUIET_START.hour, minute=QUIET_START.minute, second=0, microsecond=0
        )

    # start_by: push forward to the morning.
    if moment.timetz().replace(tzinfo=None) >= QUIET_START:
        morning = (moment + timedelta(days=1)).replace(
            hour=QUIET_END.hour, minute=QUIET_END.minute, second=0, microsecond=0
        )
        return morning
    return moment.replace(
        hour=QUIET_END.hour, minute=QUIET_END.minute, second=0, microsecond=0
    )


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, TIMESTAMP_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def ladder_for(conn: sqlite3.Connection, assignment_type: str) -> list[str]:
    """The offsets configured for this kind of work.

    Read from reminder_rules rather than a constant, so tuning them later is an
    edit to data. A type with no rule gets nothing — deliberately, so disabling a
    ladder is possible without special-casing it here.
    """
    row = conn.execute(
        "SELECT offsets_json FROM reminder_rules "
        "WHERE scope = 'assignment_type' AND assignment_type = ? AND enabled = 1",
        (assignment_type,),
    ).fetchone()
    return json.loads(row["offsets_json"]) if row else []


def rungs_for(
    due_at: datetime,
    offsets: list[str],
    zone: ZoneInfo,
    start_by: datetime | None = None,
    now: datetime | None = None,
) -> list[Rung]:
    """Every moment this assignment should produce a nudge.

    Rungs already in the past are dropped. Something entered three days before it
    is due should not fire its "seven days out" nudge the moment it is saved — an
    alert for a deadline that has already half-arrived is noise, and noise is what
    stops the useful ones being read.
    """
    now = now or datetime.now(timezone.utc)
    local_due = due_at.astimezone(zone)
    moments: list[Rung] = []

    if start_by is not None:
        moments.append(Rung(fire_at=start_by.astimezone(zone), kind="start_by"))

    for offset in offsets:
        if offset == MORNING_OF:
            when = local_due.replace(
                hour=MORNING_OF_AT.hour, minute=MORNING_OF_AT.minute,
                second=0, microsecond=0,
            )
        elif offset == NIGHT_BEFORE:
            when = (local_due - timedelta(days=1)).replace(
                hour=NIGHT_BEFORE_AT.hour, minute=NIGHT_BEFORE_AT.minute,
                second=0, microsecond=0,
            )
        else:
            delta = parse_offset(offset)
            if delta is None:
                continue
            when = local_due - delta

        moments.append(Rung(fire_at=when, kind="due_by"))

    shifted = [
        Rung(fire_at=shift_out_of_quiet_hours(rung.fire_at, rung.kind), kind=rung.kind)
        for rung in moments
    ]

    # Drop anything already past, and anything at or after the deadline itself —
    # a "due soon" alert arriving after the due time is worse than none.
    live = [
        rung for rung in shifted
        if rung.fire_at > now.astimezone(zone) and rung.fire_at < local_due
    ]

    # Same moment twice, from two rungs colliding after the quiet-hours shift,
    # would be two identical alerts on the phone.
    seen: set[str] = set()
    unique: list[Rung] = []
    for rung in sorted(live, key=lambda r: r.fire_at):
        stamp = rung.fire_at.astimezone(timezone.utc).strftime(TIMESTAMP_FMT)
        if stamp not in seen:
            seen.add(stamp)
            unique.append(rung)
    return unique


def needs_generation(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Assignments with a deadline, still outstanding, and no live reminders."""
    return conn.execute(
        f"""
        SELECT a.id, a.title, a.type, a.due_at, a.start_by, a.status
        FROM assignments a
        WHERE a.due_at IS NOT NULL
          AND a.status NOT IN ({','.join('?' * len(DONE_STATUSES))})
          AND a.feed_missing_since IS NULL
          AND NOT EXISTS (
                SELECT 1 FROM reminder_instances r
                WHERE r.assignment_id = a.id
                  AND r.state IN ({','.join('?' * len(LIVE_STATES))})
          )
        ORDER BY a.due_at
        """,
        (*DONE_STATUSES, *LIVE_STATES),
    ).fetchall()


def generate_for(
    conn: sqlite3.Connection,
    assignment: sqlite3.Row,
    zone: ZoneInfo,
    now: datetime | None = None,
) -> int:
    """Write one row per rung. Returns how many were created."""
    due = _parse(assignment["due_at"])
    if due is None:
        return 0

    offsets = ladder_for(conn, assignment["type"])
    start_by = _parse(assignment["start_by"])
    rule = conn.execute(
        "SELECT id FROM reminder_rules WHERE scope = 'assignment_type' "
        "AND assignment_type = ?",
        (assignment["type"],),
    ).fetchone()

    created = 0
    for rung in rungs_for(due, offsets, zone, start_by, now):
        conn.execute(
            "INSERT INTO reminder_instances "
            "(assignment_id, rule_id, kind, fire_at, channel, state) "
            "VALUES (?,?,?,?, 'caldav', 'pending')",
            (
                assignment["id"],
                rule["id"] if rule else None,
                rung.kind,
                rung.fire_at.astimezone(timezone.utc).strftime(TIMESTAMP_FMT),
            ),
        )
        created += 1

    if created:
        log.info(
            "Built %d reminder(s) for %r.", created, str(assignment["title"])[:60]
        )
    return created


def supersede_for(conn: sqlite3.Connection, assignment_id: int) -> int:
    """Retire every live reminder for an assignment (SPEC §5).

    Marked rather than deleted, and replaced rather than moved, so the record of
    what was scheduled and what actually went out survives a deadline change.
    """
    cur = conn.execute(
        f"UPDATE reminder_instances SET state = 'superseded' "
        f"WHERE assignment_id = ? AND state IN ({','.join('?' * len(LIVE_STATES))})",
        (assignment_id, *LIVE_STATES),
    )
    return cur.rowcount or 0


def generate_all(
    conn: sqlite3.Connection, zone: ZoneInfo, now: datetime | None = None
) -> int:
    """Build ladders for everything that is missing one."""
    return sum(
        generate_for(conn, assignment, zone, now)
        for assignment in needs_generation(conn)
    )
