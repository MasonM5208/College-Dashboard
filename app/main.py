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
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import canvas, config, db, migrate, priority, scheduler, status

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
