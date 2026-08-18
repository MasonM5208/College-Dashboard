"""The web application.

M0 serves one page. It is the "hello world" page SPEC §12 asks for, but a useful
one: it reports the schema version, confirms WAL and FTS5, and shows the
last-successful-sync table that SPEC §4 wants visible from the start.

The application does not apply migrations. The container entrypoint does that
before uvicorn starts, so a schema problem surfaces as a failure to start rather
than as a half-working page. What this module does do is *refuse to serve* if the
schema is behind, which is the same reasoning applied to the case where someone
starts uvicorn by hand.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import (canvas, claude_chat, config, db, entry, migrate, priority,
                 reminders, scheduler, status)

log = logging.getLogger("dashboard")


def configure_logging() -> None:
    """Send this project's log messages to the container's output.

    Without this, nothing below WARNING is ever seen. uvicorn configures its own
    `uvicorn.*` loggers and leaves the root logger untouched, so the root stays at
    WARNING with no handler attached, and every log.info in this package is
    discarded. Python's handler of last resort still prints warnings and errors,
    which produces the worst possible arrangement: a failing Canvas poll is
    reported and a successful one is not.

    SPEC §4 asks that every scheduled job log its outcome, precisely so that a job
    which quietly stopped can be told apart from one that is working. Only seeing
    failures makes silence ambiguous.

    The format matches app/migrate.py, so the entrypoint's migration output and the
    server's output read as one log.
    """
    # Attaches a handler and sets the root level — but only when nothing else has
    # configured logging already, which is why the levels below are set explicitly
    # rather than relied upon from here.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    # Set this project's own loggers directly. A logger's level is consulted on the
    # logger the message came from, not on its ancestors, so this holds whatever
    # the root logger's level happens to be — which matters because something else
    # may already have configured logging by the time this runs, making the call
    # above do nothing at all.
    for name in ("dashboard", "canvas", "scheduler", "migrate"):
        logging.getLogger(name).setLevel(logging.INFO)


configure_logging()

STATIC_DIR = config.REPO_ROOT / "app" / "static"
TEMPLATES_DIR = config.REPO_ROOT / "app" / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def display_zone() -> ZoneInfo:
    """The timezone the interface shows times in, falling back to UTC loudly."""
    try:
        return ZoneInfo(config.TZ)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning(
            "TZ=%r is not a timezone name this system recognises, so times will be "
            "shown in UTC. Set TZ in the .env file to a name from the tz database, "
            "for example America/Indiana/Indianapolis.",
            config.TZ,
        )
        return ZoneInfo("UTC")


def local_time(value: str | None, zone: ZoneInfo) -> str:
    """Render a stored UTC timestamp for a human, in the display timezone."""
    when = status.parse_timestamp(value)
    if when is None:
        return "never"
    return when.astimezone(zone).strftime("%a %-d %b %Y, %-I:%M %p")


def describe_age(hours: float | None) -> str:
    if hours is None:
        return "no successful run yet"
    if hours < 1:
        return f"{int(hours * 60)} minutes ago"
    if hours < 48:
        return f"{hours:.1f} hours ago"
    return f"{hours / 24:.1f} days ago"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Verify the database before serving a single request (SPEC §4)."""
    conn = db.connect()
    try:
        db.require_fts5(conn)

        mode = db.journal_mode(conn)
        if mode != "wal":
            raise RuntimeError(
                f"The database is in {mode!r} mode, not WAL. WAL is what makes the "
                f"nightly backup safe to take while the dashboard is running "
                f"(SPEC §11)."
            )

        outstanding = migrate.pending(conn)
        if outstanding:
            names = ", ".join(m.filename for m in outstanding)
            raise RuntimeError(
                f"The database schema is behind the code. Unapplied migrations: "
                f"{names}. Apply them with 'sudo docker compose run --rm app "
                f"python -m app.migrate', then start the dashboard again. See "
                f"docs/OPERATIONS.md."
            )

        app.state.schema_version = migrate.current_version(conn)
    finally:
        conn.close()

    app.state.started_at = datetime.now(timezone.utc)
    app.state.zone = display_zone()
    log.info(
        "Dashboard ready. Schema version %04d, database at %s.",
        app.state.schema_version,
        config.DB_PATH,
    )

    app.state.tasks = scheduler.start(app)
    try:
        yield
    finally:
        await scheduler.stop(app.state.tasks)


app = FastAPI(
    title="Semester Dashboard",
    # No interactive API docs: this is a single-user private application, and the
    # generated pages would be the only part of it nobody maintains.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/status")
def status_page(request: Request):
    conn = db.connect()
    try:
        facts = status.collect(conn)
    finally:
        conn.close()

    zone = request.app.state.zone
    for source in facts["sync_sources"]:
        source["last_success_display"] = local_time(source["last_success_at"], zone)
        source["last_attempt_display"] = local_time(source["last_attempt_at"], zone)
        source["age_display"] = describe_age(source["hours_since_success"])

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "facts": facts,
            "now_display": local_time(facts["now"], zone),
            "started_display": local_time(
                request.app.state.started_at.strftime(status.TIMESTAMP_FMT), zone
            ),
        },
    )



# --- the Today view ---------------------------------------------------------

# SPEC §9: one-tap estimates. These are the sizes that cover almost everything;
# anything unusual gets edited properly once M2's second half lands.
ESTIMATE_CHOICES = [
    ("15m", 0.25), ("30m", 0.5), ("1h", 1.0),
    ("2h", 2.0), ("4h", 4.0), ("8h", 8.0),
]

# How far ahead "later" reaches before an item stops being shown on the main
# screen. Long enough to see a paper coming, short enough that the page stays
# readable at a glance.
HORIZON_DAYS = 21

# At most this many estimate prompts at once. A fresh Canvas import arrives with
# nothing estimated, and showing all of them turns the default screen into a wall
# of forms with no ranked work visible at all — which fails the one-glance test
# SPEC §12 sets for this milestone. They are asked for soonest-due first, so
# clearing the visible ones is also the right order to clear them in.
ESTIMATE_PROMPT_LIMIT = 5


def _assignment_rows(conn):
    return conn.execute(
        """
        SELECT a.id, a.title, a.type, a.status, a.due_at, a.pinned,
               a.est_hours, a.est_hours_remaining,
               c.name AS course_name, c.code AS course_code
        FROM assignments a
        LEFT JOIN courses c ON c.id = a.course_id
        """
    ).fetchall()


@app.get("/")
def today(request: Request):
    """The default screen. SPEC §12: "If it takes more than one tap to know what
    to do next, this milestone is not done."

    Ordered by slack, not by deadline, and every item carries the numbers that put
    it where it is (SPEC §9 display rules).
    """
    conn = db.connect()
    try:
        rows = _assignment_rows(conn)
        facts = status.collect(conn)
        courses_to_name = conn.execute(
            "SELECT id, name, code FROM courses WHERE needs_naming = 1 ORDER BY name"
        ).fetchall()
    finally:
        conn.close()

    zone = request.app.state.zone
    now = datetime.now(timezone.utc)
    items = priority.rank(rows, zone, now)

    all_needing_estimate = [i for i in items if i.needs_estimate and i.due_at]
    needs_estimate = all_needing_estimate[:ESTIMATE_PROMPT_LIMIT]
    no_due_date = [i for i in items if not i.due_at]
    overdue = [i for i in items if i.overdue and not i.needs_estimate]
    horizon = now + timedelta(days=HORIZON_DAYS)
    ranked = [
        i for i in items
        if i.rankable and not i.overdue and i.due_local and i.due_local <= horizon
    ]
    beyond = [
        i for i in items
        if i.rankable and not i.overdue and i.due_local and i.due_local > horizon
    ]

    def present(item):
        return {
            "item": item,
            "due_text": priority.describe_due(item, now),
            "slack_text": priority.describe_slack(item),
            "due_exact": local_time(item.due_at, zone),
        }

    return templates.TemplateResponse(
        request=request,
        name="today.html",
        context={
            "facts": facts,
            "needs_estimate": [present(i) for i in needs_estimate],
            "estimates_hidden": len(all_needing_estimate) - len(needs_estimate),
            "overdue": [present(i) for i in overdue],
            "ranked": [present(i) for i in ranked],
            "beyond": [present(i) for i in beyond],
            "no_due_date": [present(i) for i in no_due_date],
            "courses_to_name": courses_to_name,
            "estimates": ESTIMATE_CHOICES,
            "now_display": local_time(now.strftime(status.TIMESTAMP_FMT), zone),
            "horizon_days": HORIZON_DAYS,
        },
    )


def _back(request: Request) -> RedirectResponse:
    """Return to wherever the button was pressed, so context is not lost."""
    target = request.headers.get("referer") or "/"
    if "://" in target:
        # Only ever redirect within this site.
        from urllib.parse import urlparse
        parsed = urlparse(target)
        target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    return RedirectResponse(target or "/", status_code=303)


@app.post("/assignments/{assignment_id}/status")
def set_status(assignment_id: int, request: Request, value: str = Form(...)):
    """One tap to change what state something is in (SPEC §12)."""
    if value not in {"not_started", "in_progress", "submitted", "dismissed"}:
        raise HTTPException(status_code=400, detail="Unknown status")

    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT status FROM assignments WHERE id = ?", (assignment_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such assignment")

        conn.execute("BEGIN")
        conn.execute(
            "UPDATE assignments SET status = ? WHERE id = ?", (value, assignment_id)
        )
        if value in ("submitted", "dismissed"):
            # Nothing is left to do, so the ranking should stop reserving time for
            # it. est_hours is left alone as the record of what was estimated.
            conn.execute(
                "UPDATE assignments SET est_hours_remaining = 0 WHERE id = ?",
                (assignment_id,),
            )
        conn.execute(
            "INSERT INTO audit_log (action, table_name, record_id, detail_json) "
            "VALUES ('status_change', 'assignments', ?, ?)",
            (assignment_id, json.dumps({"from": row["status"], "to": value})),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()

    return _back(request)


@app.post("/assignments/{assignment_id}/estimate")
def set_estimate(assignment_id: int, request: Request, hours: float = Form(...)):
    """One tap to say how long something will take.

    SPEC §9: "The prioritization engine is inert without this field populated."
    Both columns are set, because until work is logged against it in M6 the
    remaining time is the whole estimate.
    """
    if not 0 < hours <= 100:
        raise HTTPException(status_code=400, detail="Estimate out of range")

    conn = db.connect()
    try:
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE assignments SET est_hours = ?, est_hours_remaining = ? WHERE id = ?",
            (hours, hours, assignment_id),
        )
        conn.execute(
            "INSERT INTO audit_log (action, table_name, record_id, detail_json) "
            "VALUES ('estimate', 'assignments', ?, ?)",
            (assignment_id, json.dumps({"est_hours": hours})),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()

    return _back(request)


@app.post("/assignments/{assignment_id}/pin")
def toggle_pin(assignment_id: int, request: Request):
    """SPEC §9: the manual override that always wins."""
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE assignments SET pinned = 1 - pinned WHERE id = ?", (assignment_id,)
        )
    finally:
        conn.close()
    return _back(request)


@app.post("/capture")
def capture(request: Request, text: str = Form(...)):
    """Quick capture. SPEC §9: "Dump anything, triage later."

    Stored as an assignment with nothing but a title, which is what puts it in the
    needs-triage list rather than the ranking. Entry friction is what kills systems
    like this, so nothing else is asked for.
    """
    title = text.strip()
    if not title:
        return _back(request)

    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO assignments (title, source, status) "
            "VALUES (?, 'manual', 'not_started')",
            (title[:500],),
        )
        conn.execute(
            "INSERT INTO audit_log (action, table_name, record_id, detail_json) "
            "VALUES ('capture', 'assignments', ?, ?)",
            (cur.lastrowid, json.dumps({"title": title[:500]})),
        )
    finally:
        conn.close()
    return _back(request)


@app.post("/courses/{course_id}/name")
def rename_course(course_id: int, request: Request, name: str = Form(...)):
    """Replace the SIS code the feed supplied with something readable."""
    new_name = name.strip()
    if not new_name:
        return _back(request)

    conn = db.connect()
    try:
        conn.execute(
            "UPDATE courses SET name = ?, needs_naming = 0 WHERE id = ?",
            (new_name[:200], course_id),
        )
    finally:
        conn.close()
    return _back(request)



# --- entering things by hand ------------------------------------------------
#
# Canvas only carries work that has a due date set in it, which leaves most of
# Mason's semester invisible (SPEC §6.3). These are the paths by which the rest
# gets in.


def _courses(conn):
    return conn.execute(
        "SELECT id, name, code, needs_naming FROM courses ORDER BY needs_naming DESC, name"
    ).fetchall()


def _default_term_id(conn) -> int:
    """The term new courses join, creating one if the database is empty.

    Terms exist mainly for M6's capacity model. Until then a course needs one
    because courses.term_id is NOT NULL, and asking Mason to invent a term before
    he can add a course would be friction for nothing.
    """
    row = conn.execute("SELECT id FROM terms ORDER BY start_date DESC LIMIT 1").fetchone()
    if row:
        return int(row["id"])

    today = datetime.now(timezone.utc).date()
    cur = conn.execute(
        "INSERT INTO terms (name, start_date, end_date, needs_dates) VALUES (?,?,?,1)",
        ("Current term", today.isoformat(), (today + timedelta(days=120)).isoformat()),
    )
    return int(cur.lastrowid)


@app.get("/add")
def add_form(request: Request, error: str | None = None, title: str = ""):
    conn = db.connect()
    try:
        courses = _courses(conn)
    finally:
        conn.close()

    return templates.TemplateResponse(
        request=request,
        name="add.html",
        context={
            "courses": courses,
            "types": entry.ASSIGNMENT_TYPES,
            "default_hours": entry.DEFAULT_HOURS_BY_TYPE,
            "error": error,
            "title_value": title,
        },
    )


@app.post("/add")
def add_assignment(
    request: Request,
    title: str = Form(...),
    course_id: str = Form(""),
    type: str = Form("other"),
    due: str = Form(""),
    hours: str = Form(""),
    points: str = Form(""),
):
    """Create one assignment.

    SPEC §9 asks for the estimate on every create, so the form arrives with a
    per-type default already filled in and visible rather than silently applied.
    """
    zone = request.app.state.zone
    clean_title = title.strip()
    if not clean_title:
        return RedirectResponse("/add?error=Give+it+a+title", status_code=303)

    if type not in entry.ASSIGNMENT_TYPES:
        type = "other"

    try:
        due_at = entry.parse_when(due, zone)
        est_hours = entry.parse_hours(hours)
    except entry.EntryError as exc:
        from urllib.parse import quote
        return RedirectResponse(
            f"/add?error={quote(str(exc))}&title={quote(clean_title)}", status_code=303
        )

    points_possible = None
    if points.strip():
        try:
            points_possible = float(points)
        except ValueError:
            points_possible = None

    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO assignments (course_id, title, type, due_at, start_by, "
            "est_hours, est_hours_remaining, points_possible, status, source) "
            "VALUES (?,?,?,?,?,?,?,?,'not_started','manual')",
            (
                int(course_id) if course_id else None,
                clean_title[:300],
                type,
                due_at,
                entry.start_by_for(due_at, est_hours, type),
                est_hours,
                est_hours,
                points_possible,
            ),
        )
        conn.execute(
            "INSERT INTO audit_log (action, table_name, record_id, detail_json) "
            "VALUES ('create', 'assignments', ?, ?)",
            (cur.lastrowid, json.dumps({"title": clean_title[:300], "source": "manual"})),
        )
    finally:
        conn.close()

    return RedirectResponse("/", status_code=303)


@app.get("/assignments/{assignment_id}/edit")
def edit_form(assignment_id: int, request: Request, error: str | None = None):
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM assignments WHERE id = ?", (assignment_id,)
        ).fetchone()
        courses = _courses(conn)
    finally:
        conn.close()

    if row is None:
        raise HTTPException(status_code=404, detail="No such assignment")

    zone = request.app.state.zone
    due_local = ""
    if row["due_at"]:
        parsed = status.parse_timestamp(row["due_at"])
        if parsed:
            due_local = parsed.astimezone(zone).strftime("%Y-%m-%d %H:%M")

    return templates.TemplateResponse(
        request=request,
        name="edit.html",
        context={
            "row": row,
            "courses": courses,
            "types": entry.ASSIGNMENT_TYPES,
            "due_local": due_local,
            "error": error,
        },
    )


@app.post("/assignments/{assignment_id}/edit")
def edit_assignment(
    assignment_id: int,
    request: Request,
    title: str = Form(...),
    course_id: str = Form(""),
    type: str = Form("other"),
    due: str = Form(""),
    hours: str = Form(""),
):
    """Fill in what a captured note or an unmatched feed item was missing."""
    zone = request.app.state.zone
    clean_title = title.strip()
    if not clean_title:
        return RedirectResponse(
            f"/assignments/{assignment_id}/edit?error=Give+it+a+title", status_code=303
        )
    if type not in entry.ASSIGNMENT_TYPES:
        type = "other"

    try:
        due_at = entry.parse_when(due, zone)
        est_hours = entry.parse_hours(hours)
    except entry.EntryError as exc:
        from urllib.parse import quote
        return RedirectResponse(
            f"/assignments/{assignment_id}/edit?error={quote(str(exc))}", status_code=303
        )

    conn = db.connect()
    try:
        existing = conn.execute(
            "SELECT title, due_at FROM assignments WHERE id = ?", (assignment_id,)
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="No such assignment")

        conn.execute("BEGIN")
        conn.execute(
            "UPDATE assignments SET title = ?, course_id = ?, type = ?, due_at = ?, "
            "start_by = ?, est_hours = ?, est_hours_remaining = ? WHERE id = ?",
            (
                clean_title[:300],
                int(course_id) if course_id else None,
                type,
                due_at,
                entry.start_by_for(due_at, est_hours, type),
                est_hours,
                est_hours,
                assignment_id,
            ),
        )
        if existing["due_at"] != due_at:
            # SPEC §5: a changed deadline retires its reminders rather than moving
            # them, the same rule ingestion follows. The next sync rebuilds them.
            reminders.supersede_for(conn, assignment_id)
        conn.execute(
            "INSERT INTO audit_log (action, table_name, record_id, detail_json) "
            "VALUES ('edit', 'assignments', ?, ?)",
            (assignment_id, json.dumps({"title": clean_title[:300], "due_at": due_at})),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()

    return RedirectResponse("/", status_code=303)


# --- pasting a syllabus -----------------------------------------------------


@app.get("/batch")
def batch_form(request: Request):
    conn = db.connect()
    try:
        courses = _courses(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(
        request=request,
        name="batch.html",
        context={"courses": courses, "lines": None, "pasted": "", "course_id": ""},
    )


@app.post("/batch")
def batch_preview(request: Request, pasted: str = Form(""), course_id: str = Form("")):
    """Show what a pasted syllabus would become, before saving any of it.

    Nothing is written here. The preview is re-parsed from the same text on save,
    so what is shown and what is stored cannot drift apart.
    """
    zone = request.app.state.zone
    conn = db.connect()
    try:
        courses = _courses(conn)
    finally:
        conn.close()

    # Dates are shown in Mason's own timezone, not as the stored UTC. A deadline
    # of 23:59 local is 03:59 the next day in UTC, so echoing the stored string
    # back would show a different date from the one he typed.
    previewed = []
    for line in entry.parse_batch(pasted, zone):
        when = status.parse_timestamp(line.due_at)
        previewed.append({
            "line": line,
            "due_display": when.astimezone(zone).strftime("%a %-d %b %Y") if when else None,
        })

    return templates.TemplateResponse(
        request=request,
        name="batch.html",
        context={
            "courses": courses,
            "lines": previewed,
            "pasted": pasted,
            "course_id": course_id,
        },
    )


@app.post("/batch/save")
def batch_save(request: Request, pasted: str = Form(""), course_id: str = Form("")):
    """Save the readable lines. Lines with errors are left for another attempt."""
    zone = request.app.state.zone
    lines = [line for line in entry.parse_batch(pasted, zone) if line.ok]
    if not lines:
        return RedirectResponse("/batch", status_code=303)

    conn = db.connect()
    try:
        conn.execute("BEGIN")
        for line in lines:
            cur = conn.execute(
                "INSERT INTO assignments (course_id, title, type, due_at, start_by, "
                "est_hours, est_hours_remaining, status, source) "
                "VALUES (?,?,?,?,?,?,?,'not_started','syllabus_batch')",
                (
                    int(course_id) if course_id else None,
                    line.title,
                    line.type,
                    line.due_at,
                    entry.start_by_for(line.due_at, line.est_hours, line.type),
                    line.est_hours,
                    line.est_hours,
                ),
            )
            conn.execute(
                "INSERT INTO audit_log (action, table_name, record_id, detail_json) "
                "VALUES ('syllabus_batch', 'assignments', ?, ?)",
                (cur.lastrowid, json.dumps({"title": line.title})),
            )
        conn.execute("COMMIT")
    finally:
        conn.close()

    log.info("Added %d assignments from a pasted syllabus.", len(lines))
    return RedirectResponse("/", status_code=303)


# --- courses ----------------------------------------------------------------


@app.get("/courses")
def courses_page(request: Request):
    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT c.*, COUNT(a.id) AS assignment_count
            FROM courses c
            LEFT JOIN assignments a ON a.course_id = c.id
            GROUP BY c.id
            ORDER BY c.needs_naming DESC, c.name
            """
        ).fetchall()
    finally:
        conn.close()

    return templates.TemplateResponse(
        request=request, name="courses.html", context={"courses": rows}
    )


@app.post("/courses")
def create_course(
    request: Request,
    name: str = Form(...),
    code: str = Form(""),
    instructor: str = Form(""),
    meeting_pattern: str = Form(""),
    credits: str = Form(""),
    late_policy: str = Form(""),
):
    """Add a course Canvas knows nothing about.

    Only a name is required. The rest matters to M6's overload mode, which ranks
    what is cheapest to sacrifice, and can be filled in whenever the syllabus is
    to hand.
    """
    clean_name = name.strip()
    if not clean_name:
        return RedirectResponse("/courses", status_code=303)

    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO courses (term_id, name, code, instructor, meeting_pattern, "
            "credits, late_policy, needs_naming) VALUES (?,?,?,?,?,?,?,0)",
            (
                _default_term_id(conn),
                clean_name[:200],
                code.strip()[:60] or None,
                instructor.strip()[:120] or None,
                meeting_pattern.strip()[:120] or None,
                float(credits) if credits.strip().replace(".", "", 1).isdigit() else None,
                late_policy.strip()[:500] or None,
            ),
        )
    finally:
        conn.close()

    return RedirectResponse("/courses", status_code=303)


@app.post("/courses/{course_id}/edit")
def edit_course(
    course_id: int,
    request: Request,
    name: str = Form(...),
    code: str = Form(""),
    instructor: str = Form(""),
    meeting_pattern: str = Form(""),
    credits: str = Form(""),
    late_policy: str = Form(""),
    current_grade_pct: str = Form(""),
):
    clean_name = name.strip()
    if not clean_name:
        return _back(request)

    def number(text):
        text = text.strip()
        try:
            return float(text) if text else None
        except ValueError:
            return None

    grade = number(current_grade_pct)
    if grade is not None and not 0 <= grade <= 100:
        grade = None

    conn = db.connect()
    try:
        conn.execute(
            "UPDATE courses SET name = ?, code = ?, instructor = ?, meeting_pattern = ?, "
            "credits = ?, late_policy = ?, current_grade_pct = ?, needs_naming = 0 "
            "WHERE id = ?",
            (
                clean_name[:200],
                code.strip()[:60] or None,
                instructor.strip()[:120] or None,
                meeting_pattern.strip()[:120] or None,
                number(credits),
                late_policy.strip()[:500] or None,
                grade,
                course_id,
            ),
        )
    finally:
        conn.close()

    return _back(request)



# --- chat -------------------------------------------------------------------
#
# SPEC §10: one endpoint, tool-based routing, no intent classifier. Whether a
# question is about deadlines or about coursework is Claude's decision, made by
# choosing a tool, not ours made by inspecting the text.


def _thread_messages(conn, thread_id: int):
    return conn.execute(
        "SELECT * FROM chat_messages WHERE thread_id = ? ORDER BY id", (thread_id,)
    ).fetchall()


def _history_for_model(rows) -> list[dict]:
    """The conversation so far, in the shape the API expects.

    Only the user and assistant text is replayed. Tool calls and their results are
    kept in the database for auditing, but a finished exchange does not need them
    re-sent — the answer already reflects what they returned.
    """
    history = []
    for row in rows:
        if row["role"] == "user" and row["content"]:
            history.append({"role": "user", "content": row["content"]})
        elif row["role"] == "assistant" and row["content"]:
            history.append({"role": "assistant", "content": row["content"]})
    return history


@app.get("/chat")
def chat_page(request: Request, thread: int | None = None):
    conn = db.connect()
    try:
        threads = conn.execute(
            "SELECT id, title, updated_at FROM chat_threads ORDER BY updated_at DESC LIMIT 30"
        ).fetchall()

        thread_id = thread or (threads[0]["id"] if threads else None)
        messages = _thread_messages(conn, thread_id) if thread_id else []
        spend = claude_chat.month_to_date_cost(conn)
        configured = bool(os.environ.get("CLAUDE_API_KEY", "").strip())
    finally:
        conn.close()

    zone = request.app.state.zone
    shown = [
        {
            "role": row["role"],
            "content": row["content"],
            # Rendered here rather than in the template so the escaping rules live
            # in one place. Questions stay plain text; only replies are Markdown.
            "html": (
                claude_chat.render_markdown(row["content"])
                if row["role"] == "assistant"
                else None
            ),
            "thinking": row["thinking"],
            "when": local_time(row["created_at"], zone),
            "tokens": row["input_tokens"] + row["output_tokens"],
            "cost": claude_chat.message_cost(row),
            "model": row["model"],
        }
        for row in messages
        if row["role"] in ("user", "assistant")
    ]

    # Answer the newest question if it has not been answered yet — that is what
    # the page's event stream connects to.
    pending = bool(messages) and messages[-1]["role"] == "user"

    return templates.TemplateResponse(
        request=request,
        name="chat.html",
        context={
            "threads": threads,
            "thread_id": thread_id,
            "messages": shown,
            "pending": pending,
            "spend": spend,
            "configured": configured,
            "model": config.CHAT_MODEL,
        },
    )


@app.post("/chat/send")
def chat_send(request: Request, question: str = Form(...), thread: str = Form("")):
    text = question.strip()
    if not text:
        return RedirectResponse("/chat", status_code=303)

    conn = db.connect()
    try:
        thread_id = int(thread) if thread.strip().isdigit() else None
        if thread_id is None:
            cur = conn.execute(
                "INSERT INTO chat_threads (title) VALUES (?)", (text[:80],)
            )
            thread_id = int(cur.lastrowid)

        conn.execute(
            "INSERT INTO chat_messages (thread_id, role, content) VALUES (?, 'user', ?)",
            (thread_id, text[:8000]),
        )
    finally:
        conn.close()

    return RedirectResponse(f"/chat?thread={thread_id}", status_code=303)


@app.get("/chat/{thread_id}/stream")
def chat_stream(thread_id: int, request: Request):
    """Answer the newest question, streaming the reply as it is written.

    Server-sent events. Streaming is not only for appearance: it is also what
    stops a long answer from hitting an HTTP timeout.
    """
    zone = request.app.state.zone

    def events():
        conn = db.connect()
        try:
            rows = _thread_messages(conn, thread_id)
            if not rows or rows[-1]["role"] != "user":
                yield "event: done\ndata: {}\n\n"
                return

            history = _history_for_model(rows)
            turn = None
            try:
                for kind, payload in claude_chat.answer(conn, history, zone):
                    if kind == "done":
                        turn = payload
                    else:
                        yield f"event: {kind}\ndata: {json.dumps(payload)}\n\n"
            except claude_chat.ChatUnavailable as exc:
                yield f"event: failed\ndata: {json.dumps(str(exc))}\n\n"
                return
            except Exception as exc:  # noqa: BLE001
                log.exception("Chat request failed")
                # The message is deliberately generic: an API error can quote the
                # request, and the request carries the key.
                yield (
                    "event: failed\ndata: "
                    + json.dumps(
                        "Something went wrong talking to Claude. The server log has "
                        "the detail."
                    )
                    + "\n\n"
                )
                return

            conn.execute(
                "INSERT INTO chat_messages (thread_id, role, content, thinking, "
                "tool_calls, tool_results, model, stop_reason, input_tokens, "
                "output_tokens, cache_read_tokens, cache_write_tokens) "
                "VALUES (?, 'assistant', ?,?,?,?,?,?,?,?,?,?)",
                (
                    thread_id,
                    turn.text,
                    turn.thinking or None,
                    json.dumps(turn.tool_calls) if turn.tool_calls else None,
                    json.dumps(turn.tool_results) if turn.tool_results else None,
                    turn.model,
                    turn.stop_reason,
                    turn.input_tokens,
                    turn.output_tokens,
                    turn.cache_read_tokens,
                    turn.cache_write_tokens,
                ),
            )
            yield "event: done\ndata: {}\n\n"
        finally:
            conn.close()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/assignments")
def assignments(request: Request):
    """Everything ingested, grouped by course.

    Deliberately a plain list ordered by due date. The Today view — which ranks by
    slack rather than by deadline, per SPEC §9 — is M2. This page exists to prove
    the feed is arriving and to show what needs attention.
    """
    conn = db.connect()
    try:
        facts = status.collect(conn)
        rows = conn.execute(
            """
            SELECT a.id, a.title, a.type, a.due_at, a.status, a.source,
                   a.feed_missing_since, a.est_hours_remaining,
                   c.id AS course_id, c.name AS course_name,
                   c.code AS course_code, c.needs_naming
            FROM assignments a
            LEFT JOIN courses c ON c.id = a.course_id
            ORDER BY (a.due_at IS NULL), a.due_at, a.title
            """
        ).fetchall()
    finally:
        conn.close()

    zone = request.app.state.zone
    groups: dict[object, dict] = {}
    for row in rows:
        key = row["course_id"]
        group = groups.setdefault(
            key,
            {
                "course_id": key,
                "name": row["course_name"] or "Not yet matched to a course",
                "code": row["course_code"],
                "needs_naming": bool(row["needs_naming"]),
                "unmatched": key is None,
                "items": [],
            },
        )
        group["items"].append(
            {
                "title": row["title"],
                "type": row["type"],
                "status": row["status"],
                "source": row["source"],
                "due_display": local_time(row["due_at"], zone),
                "due_at": row["due_at"],
                "missing_since": row["feed_missing_since"],
                "missing_display": local_time(row["feed_missing_since"], zone),
                "est_hours_remaining": row["est_hours_remaining"],
            }
        )

    # Unmatched items first, since they are the group that needs Mason.
    ordered = sorted(groups.values(), key=lambda g: (not g["unmatched"], g["name"]))

    return templates.TemplateResponse(
        request=request,
        name="assignments.html",
        context={"facts": facts, "groups": ordered, "total": len(rows)},
    )


@app.post("/sync/canvas")
def sync_canvas():
    """Poll Canvas now rather than waiting for the next scheduled run.

    Needed to test a due-date change inside one poll cycle without waiting half an
    hour, and useful whenever the feed looks stale.
    """
    conn = db.connect()
    try:
        canvas.sync(conn)
    except canvas.FeedError:
        # sync() has already recorded this in sync_state, and the status page
        # renders it. Redirecting keeps the browser on a real page either way.
        pass
    finally:
        conn.close()
    return RedirectResponse("/assignments", status_code=303)


@app.get("/healthz")
def healthz():
    """Machine-readable version of the status page.

    Used by the container health check and by the "the site won't load" decision
    tree in docs/OPERATIONS.md. Returns 503 when a check fails, so `docker ps`
    shows the container as unhealthy rather than merely running.
    """
    conn = db.connect()
    try:
        facts = status.collect(conn)
    finally:
        conn.close()
    return JSONResponse(facts, status_code=200 if facts["ok"] else 503)


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    """Served from the root so the worker's scope covers the whole site.

    A service worker may only control paths at or below its own URL, and this one
    has to cover every page for the installed iPhone app to work. Serving it from
    /static would limit it to /static.
    """
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )
