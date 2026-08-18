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

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import canvas, config, db, migrate, scheduler, status

log = logging.getLogger("dashboard")

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


@app.get("/")
def index(request: Request):
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
