"""The in-process scheduler.

SPEC §4 rules out Redis, Celery and message queues: the workload is a handful of
jobs an hour on a 1 vCPU server. This is an asyncio task and a sleep.

Why the Canvas poll lives here and the nightly backup does not: the backup has to
keep running when the container is crash-looping, which is exactly when last
night's copy matters, so it runs from cron on the host. Polling Canvas is
meaningless when the app is down, so it belongs in the app.

Every job writes to sync_state and logs its outcome (SPEC §4), which is what makes
a job that quietly stopped visible on the status page instead of being discovered
in November.
"""

from __future__ import annotations

import asyncio
import logging

from app import canvas, config, db

log = logging.getLogger("scheduler")

# SPEC §6: every 30 minutes. Canvas caches the feed server-side, so polling harder
# buys nothing — expect up to an hour of staleness regardless.
CANVAS_POLL_SECONDS = 30 * 60

# A short pause before the first poll so the web server is answering requests
# before any work starts competing with it.
STARTUP_DELAY_SECONDS = 5


def poll_canvas_once() -> canvas.SyncResult:
    """One Canvas poll, start to finish. Blocking; call it in a thread."""
    conn = db.connect()
    try:
        return canvas.sync(conn)
    finally:
        conn.close()


async def _canvas_loop() -> None:
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    while True:
        try:
            # to_thread keeps the blocking fetch and SQLite work off the event
            # loop, so the dashboard stays responsive while a poll is running.
            await asyncio.to_thread(poll_canvas_once)
        except canvas.FeedError as exc:
            # Already recorded in sync_state and shown on the status page. Logged
            # at warning rather than error because a Canvas outage is not a fault
            # of ours, and the loop keeps going.
            log.warning("Canvas poll failed: %s", exc)
        except Exception:
            log.exception("Canvas poll raised an unexpected error")
        await asyncio.sleep(CANVAS_POLL_SECONDS)


def start(app) -> list[asyncio.Task]:
    """Start background jobs, returning the tasks so shutdown can cancel them."""
    tasks: list[asyncio.Task] = []

    if not config.canvas_configured():
        log.info(
            "CANVAS_ICS_URL is not set, so the Canvas feed will not be polled. "
            "Add it to the secrets file to turn ingestion on — see docs/SECRETS.md."
        )
        return tasks

    tasks.append(asyncio.create_task(_canvas_loop(), name="canvas-poll"))
    log.info("Canvas polling every %d minutes.", CANVAS_POLL_SECONDS // 60)
    return tasks


async def stop(tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
