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

import hmac
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

from app import (archive, canvas, claude_chat, config, db, entry, mailbox,
                 migrate, priority, reminders, scheduler, status)

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



# --- the archive ------------------------------------------------------------
#
# SPEC §7: every path in — the iOS share sheet, the paste form, any future mail
# bridge — converges on archive.ingest(). The routes below are adapters; none of
# them writes to `documents` itself.


def _check_ingest_token(request: Request) -> None:
    """Authorise a machine-to-machine save.

    Tailscale already keeps strangers off the network (SPEC §11). The token is
    what stops anything *else* on the tailnet writing into the permanent archive,
    including a Shortcut pointed at the wrong address.

    Nothing here — not the response, not the log — ever contains either token.
    hmac.compare_digest rather than == because a plain comparison returns early on
    the first differing byte, and the timing of that is a slow way to read a
    secret.
    """
    if not config.ingest_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "Saving from the phone is switched off: INGEST_TOKEN is not set on "
                "the server. See docs/SETUP.md section 17."
            ),
        )

    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    expected = os.environ.get("INGEST_TOKEN", "").strip()
    if scheme.lower() != "bearer" or not hmac.compare_digest(presented.strip(), expected):
        raise HTTPException(status_code=401, detail="Not authorised.")


@app.post("/ingest")
async def ingest_endpoint(request: Request):
    """Save a message sent by the iPhone Shortcut.

    Accepts JSON or form encoding, because which of the two is easier to build in
    Shortcuts depends on the iOS version, and neither is harder to read here.

    A body that is already in the archive is a **success**, not an error: from the
    phone's point of view "it is saved" is true either way, and a red failure
    banner for sharing something twice would teach the wrong lesson about a
    feature whose whole purpose is capture without thinking.
    """
    _check_ingest_token(request)

    if "application/json" in request.headers.get("content-type", ""):
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 — any parse failure is the same answer
            raise HTTPException(status_code=400, detail="The body is not valid JSON.")
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=400, detail="Expected a JSON object with a 'body' field."
            )
    else:
        payload = dict(await request.form())

    def field(name: str) -> str | None:
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    body = payload.get("body")
    if not isinstance(body, str) or not body.strip():
        raise HTTPException(
            status_code=400,
            detail="Nothing to save: the request had no 'body' field with text in it.",
        )

    source = field("source") or "share_sheet"
    if source not in archive.SOURCES:
        raise HTTPException(status_code=400, detail=f"Unknown source {source!r}.")

    kind = field("kind") or "other"
    if kind not in archive.KINDS:
        kind = "other"

    conn = db.connect()
    try:
        result = archive.ingest(
            conn,
            body,
            source=source,
            subject=field("subject"),
            sender=field("sender"),
            received_at=field("received_at"),
            kind=kind,
            external_id=field("external_id"),
            raw_headers=field("raw_headers"),
        )
    except archive.ArchiveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    finally:
        conn.close()

    return JSONResponse(
        {
            "ok": True,
            "document_id": result.document_id,
            "created": result.created,
            "source_added": result.source_added,
            "url": f"/archive/{result.document_id}",
            # A sentence rather than a status code, because this is what the
            # Shortcut puts in the notification. Sharing something and getting
            # back raw JSON tells you it worked but not what happened; "Already
            # had this one" is the difference between trusting dedup and
            # wondering whether you have two copies.
            "message": (
                "Saved to the archive." if result.created
                else "Already had this one — nothing duplicated."
            ),
        }
    )


def _shown_documents(conn, rows, zone) -> list[dict]:
    links = archive.courses_for_many(conn, [row["id"] for row in rows])
    return [
        {
            "id": row["id"],
            "subject": row["subject"] or "(no subject)",
            "sender": row["sender"],
            "kind": row["kind"],
            # What the message says it is, falling back to when it was saved.
            # Both are shown rather than one, because "received last Tuesday,
            # saved this morning" is ordinary and the difference matters when
            # checking whether something was missed.
            "received": local_time(row["received_at"], zone) if row["received_at"] else None,
            "ingested": local_time(row["ingested_at"], zone),
            "snippet": archive.snippet_html(row["body_snippet"]),
            "courses": links.get(row["id"], []),
        }
        for row in rows
    ]


@app.get("/archive")
def archive_page(request: Request, q: str = "", course: int | None = None):
    """Search the archive, or see the newest of it when nothing is typed."""
    query = q.strip()
    conn = db.connect()
    try:
        rows = (
            archive.search(conn, query, course_id=course)
            if query
            else archive.recent(conn)
        )
        documents = _shown_documents(conn, rows, request.app.state.zone)
        total = archive.count(conn)
        courses = _courses(conn)
        configured = config.ingest_configured()
        waiting = mailbox.pending_count(conn)
    finally:
        conn.close()

    return templates.TemplateResponse(
        request=request,
        name="archive.html",
        context={
            "documents": documents,
            "query": query,
            "course_id": course,
            "courses": courses,
            "total": total,
            "ingest_configured": configured,
            "waiting": waiting,
        },
    )


# How a message's provenance reads on screen. The stored values are SPEC §5's
# enum; `gmail_poll` is its name for "collected from a mailbox", from when the
# expected provider was Gmail.
SOURCE_LABELS = {
    "share_sheet": "shared from your phone",
    "paste": "pasted in",
    "mail_bridge": "mail bridge",
    "gmail_poll": "forwarded email",
}


@app.get("/archive/review")
def review_queue(request: Request):
    """Mail collected automatically, waiting to be kept or thrown away.

    The archive is only worth searching if everything in it is something Mason
    decided mattered — SPEC §7's case for keyword search over vectors rests on
    exactly that. So collected mail stops here rather than going straight in.
    """
    conn = db.connect()
    try:
        rows = mailbox.pending(conn)
        configured = mailbox.configured()
        total = archive.count(conn)
    finally:
        conn.close()

    zone = request.app.state.zone
    return templates.TemplateResponse(
        request=request,
        name="review.html",
        context={
            "messages": [
                {
                    "id": row["id"],
                    "subject": row["subject"] or "(no subject)",
                    "sender": row["sender"],
                    "when": local_time(row["received_at"] or row["fetched_at"], zone),
                    # Enough to judge by without opening anything. The verbatim
                    # copy is what gets stored if it is kept.
                    "preview": _preview(row["body"], 400),
                }
                for row in rows
            ],
            "configured": configured,
            "total": total,
        },
    )


@app.post("/archive/review/{inbound_id}/keep")
def review_keep(inbound_id: int, request: Request):
    conn = db.connect()
    try:
        mailbox.keep(conn, inbound_id)
    except (mailbox.MailboxError, archive.ArchiveError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    finally:
        conn.close()
    return _back(request)


@app.post("/archive/review/{inbound_id}/discard")
def review_discard(inbound_id: int, request: Request):
    conn = db.connect()
    try:
        mailbox.discard(conn, inbound_id)
    finally:
        conn.close()
    return _back(request)


@app.post("/archive/review/discard-all")
def review_discard_all(request: Request):
    """Clear a backlog in one go, for the fortnight nobody looked at this."""
    conn = db.connect()
    try:
        mailbox.discard_all(conn)
    finally:
        conn.close()
    return RedirectResponse("/archive/review", status_code=303)


@app.post("/sync/mail")
def sync_mail_now():
    """Collect mail immediately rather than waiting for the next poll."""
    conn = db.connect()
    try:
        result = mailbox.sync(conn)
    except mailbox.MailboxError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
    finally:
        conn.close()

    return JSONResponse(
        {
            "ok": True,
            "fetched": result.fetched,
            "waiting_for_review": result.queued,
            "already_held": result.already_held,
            "skipped": result.skipped,
        }
    )


@app.get("/archive/add")
def archive_add_form(request: Request, error: str | None = None):
    conn = db.connect()
    try:
        courses = _courses(conn)
    finally:
        conn.close()

    return templates.TemplateResponse(
        request=request,
        name="archive_add.html",
        context={"courses": courses, "error": error, "kinds": archive.KINDS},
    )


@app.post("/archive/add")
def archive_add(
    request: Request,
    body: str = Form(""),
    subject: str = Form(""),
    sender: str = Form(""),
    received: str = Form(""),
    kind: str = Form("other"),
    course_id: str = Form(""),
):
    """The paste form. Same pipeline as the phone, different front door."""
    from urllib.parse import quote

    zone = request.app.state.zone
    try:
        received_at = entry.parse_when(received, zone)
    except entry.EntryError as exc:
        return RedirectResponse(f"/archive/add?error={quote(str(exc))}", status_code=303)

    conn = db.connect()
    try:
        result = archive.ingest(
            conn,
            body,
            source="paste",
            subject=subject,
            sender=sender,
            received_at=received_at,
            kind=kind if kind in archive.KINDS else "other",
        )
        if course_id.strip().isdigit():
            archive.link_course(conn, result.document_id, int(course_id))
    except archive.ArchiveError as exc:
        return RedirectResponse(f"/archive/add?error={quote(str(exc))}", status_code=303)
    finally:
        conn.close()

    # `saved=new` and `saved=duplicate` are different messages on the next page:
    # a duplicate is not a failure, but silently showing the same "Saved" banner
    # would hide that dedup did something.
    outcome = "new" if result.created else "duplicate"
    return RedirectResponse(
        f"/archive/{result.document_id}?saved={outcome}", status_code=303
    )


@app.get("/archive/{document_id}")
def document_page(
    request: Request,
    document_id: int,
    saved: str | None = None,
    deleting: int | None = None,
):
    """One message, exactly as it arrived.

    This is what a citation in the chat links to, so it shows the body verbatim in
    a <pre> and renders nothing: no Markdown, no link detection, no tidying. If
    the archive's copy differed in any way from what was received, the whole point
    of having it would be gone.
    """
    conn = db.connect()
    try:
        document = archive.get(conn, document_id)
        if document is None:
            raise HTTPException(status_code=404, detail="No such document")

        sources = archive.sources_for(conn, document_id)
        attached = archive.courses_for(conn, document_id)
        attached_ids = {row["id"] for row in attached}
        available = [row for row in _courses(conn) if row["id"] not in attached_ids]
    finally:
        conn.close()

    zone = request.app.state.zone
    return templates.TemplateResponse(
        request=request,
        name="document.html",
        context={
            "document": document,
            "subject": document["subject"] or "(no subject)",
            "received": (
                local_time(document["received_at"], zone)
                if document["received_at"]
                else None
            ),
            "ingested": local_time(document["ingested_at"], zone),
            "sources": [
                {
                    "source": SOURCE_LABELS.get(row["source"], row["source"]),
                    "when": local_time(row["ingested_at"], zone),
                }
                for row in sources
            ],
            "attached": attached,
            "available": available,
            "saved": saved,
            "deleting": deleting == document_id,
        },
    )


@app.post("/archive/{document_id}/link")
def link_document(document_id: int, request: Request, course_id: str = Form(...)):
    if not course_id.strip().isdigit():
        return _back(request)

    conn = db.connect()
    try:
        if archive.get(conn, document_id) is None:
            raise HTTPException(status_code=404, detail="No such document")
        archive.link_course(conn, document_id, int(course_id))
    finally:
        conn.close()
    return _back(request)


@app.post("/archive/{document_id}/unlink")
def unlink_document(document_id: int, request: Request, course_id: str = Form(...)):
    if not course_id.strip().isdigit():
        return _back(request)

    conn = db.connect()
    try:
        archive.unlink_course(conn, document_id, int(course_id))
    finally:
        conn.close()
    return _back(request)


@app.post("/archive/{document_id}/delete")
def delete_document(document_id: int, request: Request):
    """Remove a document, behind the confirmation the page renders in place.

    An immutable body does not mean an undeletable row: a mis-paste has to be
    removable. What cannot happen is a message being quietly *rewritten*, which is
    what the database trigger prevents.
    """
    conn = db.connect()
    try:
        if archive.get(conn, document_id) is None:
            raise HTTPException(status_code=404, detail="No such document")
        archive.delete(conn, document_id)
    finally:
        conn.close()
    return RedirectResponse("/archive", status_code=303)


# --- chat -------------------------------------------------------------------
#
# SPEC §10: one endpoint, tool-based routing, no intent classifier. Whether a
# question is about deadlines or about coursework is Claude's decision, made by
# choosing a tool, not ours made by inspecting the text.


# The tools whose answers must carry a citation (SPEC §10).
ARCHIVE_TOOLS = ("search_archive", "get_document")


def _uncited_archive_claim(row) -> bool:
    """Whether a reply leaned on the archive without linking to it.

    SPEC §10: "make unsourced archive claims a visible UI state". The rule is
    enforced in the system prompt, and a prompt is an instruction rather than a
    guarantee — so the page also checks. A turn that read the archive and produced
    no link to it gets a visible warning instead of quietly reading as verified.

    Computed here from the tool calls already stored on the message: no extra
    column, and nothing a later edit to the prompt can switch off by accident.
    """
    if row["role"] != "assistant" or not row["tool_calls"]:
        return False
    try:
        calls = json.loads(row["tool_calls"])
    except (TypeError, ValueError):
        return False

    used_archive = any(call.get("name") in ARCHIVE_TOOLS for call in calls)
    return used_archive and "/archive/" not in (row["content"] or "")


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


def _preview(text: str | None, limit: int = 120) -> str:
    """A one-line taste of a reply, for the conversation list.

    Titles are taken from opening questions, and opening questions look alike —
    half of them start "what". The reply is what actually tells one conversation
    from another when scanning for the one you half-remember.
    """
    if not text:
        return ""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rsplit(" ", 1)[0] + "…"


def _thread_list(conn):
    """Every conversation: kept ones first, then most recently used."""
    return conn.execute(
        """
        SELECT t.id, t.title, t.pinned, t.updated_at,
               (SELECT COUNT(*) FROM chat_messages m
                 WHERE m.thread_id = t.id AND m.role IN ('user','assistant'))
                 AS message_count,
               (SELECT m.content FROM chat_messages m
                 WHERE m.thread_id = t.id AND m.role = 'assistant'
                   AND m.content IS NOT NULL AND m.content <> ''
                 ORDER BY m.id DESC LIMIT 1) AS last_reply
          FROM chat_threads t
         ORDER BY t.pinned DESC, t.updated_at DESC
        """
    ).fetchall()


def _shown_threads(rows, zone) -> list[dict]:
    return [
        {
            "id": row["id"],
            # A conversation always has a name on screen, so the list never has a
            # blank row you have to open to identify.
            "title": row["title"] or "Untitled conversation",
            "pinned": bool(row["pinned"]),
            "message_count": row["message_count"],
            "when": local_time(row["updated_at"], zone),
            "preview": _preview(row["last_reply"]),
        }
        for row in rows
    ]


# How many conversations the chat page lists beneath the current one before
# sending you to the full list. Enough to cover "the one from this morning",
# short enough that it does not bury the cost line at the bottom of the page.
THREADS_ON_CHAT_PAGE = 8


@app.get("/chat")
def chat_page(
    request: Request,
    thread: int | None = None,
    deleting: int | None = None,
):
    """One conversation at a time, with the others listed underneath.

    With no ``?thread=`` this starts a *new* conversation rather than continuing
    the most recent one. That is the substance of the change: appending every
    question to whatever came last makes an unreadable transcript, and it is also
    billed for, because ``_history_for_model`` re-sends the entire thread on every
    turn. Unrelated questions should not be paying to carry each other.
    """
    conn = db.connect()
    try:
        rows = _thread_list(conn)
        known = {row["id"] for row in rows}

        # An id that no longer exists — a deleted conversation still open in
        # another tab, a stale bookmark — opens a new conversation rather than a
        # 404. Nothing is lost by being forgiving here.
        thread_id = thread if thread in known else None
        deleting = deleting if deleting in known else None

        messages = _thread_messages(conn, thread_id) if thread_id else []
        spend = claude_chat.month_to_date_cost(conn)
        configured = bool(os.environ.get("CLAUDE_API_KEY", "").strip())
    finally:
        conn.close()

    zone = request.app.state.zone
    threads = _shown_threads(rows, zone)
    current = next((item for item in threads if item["id"] == thread_id), None)
    others = [item for item in threads if item["id"] != thread_id]

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
            "uncited": _uncited_archive_claim(row),
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
            "current": current,
            "threads": others[:THREADS_ON_CHAT_PAGE],
            "more_threads": max(len(others) - THREADS_ON_CHAT_PAGE, 0),
            "thread_id": thread_id,
            "deleting": deleting,
            "messages": shown,
            "pending": pending,
            "spend": spend,
            "configured": configured,
            "model": config.CHAT_MODEL,
        },
    )


@app.get("/chat/threads")
def chat_threads_page(request: Request, deleting: int | None = None):
    """Every conversation ever had, with the rename, keep and delete controls.

    Separate from the chat page because the chat page's job is the conversation
    in front of you; managing the archive of them is a different task and should
    not clutter it.
    """
    conn = db.connect()
    try:
        rows = _thread_list(conn)
        if deleting not in {row["id"] for row in rows}:
            deleting = None
    finally:
        conn.close()

    return templates.TemplateResponse(
        request=request,
        name="chat_threads.html",
        context={
            "threads": _shown_threads(rows, request.app.state.zone),
            "deleting": deleting,
            "thread_id": None,
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
        if thread_id is not None:
            # A thread that has been deleted since the page was rendered must not
            # resurrect itself as an orphan row: start a fresh conversation.
            exists = conn.execute(
                "SELECT 1 FROM chat_threads WHERE id = ?", (thread_id,)
            ).fetchone()
            if exists is None:
                thread_id = None

        if thread_id is None:
            # The opening question names the conversation until it is renamed.
            # Whitespace is collapsed first so a pasted multi-line question does
            # not become a title with newlines in it.
            cur = conn.execute(
                "INSERT INTO chat_threads (title) VALUES (?)",
                (" ".join(text.split())[:80],),
            )
            thread_id = int(cur.lastrowid)

        conn.execute(
            "INSERT INTO chat_messages (thread_id, role, content) VALUES (?, 'user', ?)",
            (thread_id, text[:8000]),
        )
    finally:
        conn.close()

    return RedirectResponse(f"/chat?thread={thread_id}", status_code=303)


def _require_thread(conn, thread_id: int) -> None:
    row = conn.execute(
        "SELECT 1 FROM chat_threads WHERE id = ?", (thread_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No such conversation")


@app.post("/chat/{thread_id}/rename")
def rename_thread(thread_id: int, request: Request, title: str = Form(...)):
    """Give a conversation a name you will recognise in October."""
    name = " ".join(title.split())[:80]
    conn = db.connect()
    try:
        _require_thread(conn, thread_id)
        # An empty box clears the name rather than storing "", so the list falls
        # back to its placeholder instead of showing a blank line.
        conn.execute(
            "UPDATE chat_threads SET title = ? WHERE id = ?", (name or None, thread_id)
        )
    finally:
        conn.close()
    return _back(request)


@app.post("/chat/{thread_id}/keep")
def toggle_thread_kept(thread_id: int, request: Request):
    """Kept conversations sort above the rest, however old they get."""
    conn = db.connect()
    try:
        _require_thread(conn, thread_id)
        conn.execute(
            "UPDATE chat_threads SET pinned = 1 - pinned WHERE id = ?", (thread_id,)
        )
    finally:
        conn.close()
    return _back(request)


@app.post("/chat/{thread_id}/delete")
def delete_thread(thread_id: int, request: Request):
    """Delete a conversation and its messages for good.

    Reached only through the confirmation the list renders in place: this is the
    one button in the dashboard that destroys something, and a mis-tap on a phone
    should not be enough. The messages are deleted explicitly rather than left to
    the foreign key's ON DELETE CASCADE, which is silently a no-op on any
    connection where PRAGMA foreign_keys was not set.

    If it is pressed by mistake, last night's backup still has the conversation —
    docs/OPERATIONS.md covers reading one out of a restored copy.
    """
    conn = db.connect()
    try:
        _require_thread(conn, thread_id)
        conn.execute("DELETE FROM chat_messages WHERE thread_id = ?", (thread_id,))
        conn.execute("DELETE FROM chat_threads WHERE id = ?", (thread_id,))
    finally:
        conn.close()

    # Never back to ?thread=<the one just deleted>. Returning to the list it was
    # deleted from is the least surprising place to land.
    referer = request.headers.get("referer") or ""
    return RedirectResponse(
        "/chat/threads" if "/chat/threads" in referer else "/chat", status_code=303
    )


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
