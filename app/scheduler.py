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

from app import caldav_push, canvas, capacity, config, db, mailbox

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


def push_reminders_once() -> caldav_push.SyncResult:
    """One reminder sync, start to finish. Blocking; call it in a thread."""
    conn = db.connect()
    try:
        return caldav_push.sync(conn)
    finally:
        conn.close()


# SPEC §9's exam auto-milestones. Hourly rather than every fifteen minutes: an
# exam entered now does not need its study ladder within the quarter-hour, and a
# job that creates work should run at the slowest rate that is still useful.
STUDY_SESSION_INTERVAL_SECONDS = 60 * 60


def generate_study_sessions_once() -> int:
    conn = db.connect()
    try:
        from zoneinfo import ZoneInfo
        return capacity.generate_study_sessions(conn, ZoneInfo(config.TZ))
    finally:
        conn.close()


async def _study_session_loop() -> None:
    await asyncio.sleep(STARTUP_DELAY_SECONDS * 4)
    while True:
        try:
            await asyncio.to_thread(generate_study_sessions_once)
        except Exception:
            log.exception("Generating study sessions raised an unexpected error")
        await asyncio.sleep(STUDY_SESSION_INTERVAL_SECONDS)


async def _reminder_loop() -> None:
    await asyncio.sleep(STARTUP_DELAY_SECONDS * 2)
    while True:
        try:
            await asyncio.to_thread(push_reminders_once)
        except caldav_push.CalDavError as exc:
            # Already recorded in sync_state and shown on the status page.
            log.warning("Reminder sync failed: %s", exc)
        except Exception:
            log.exception("Reminder sync raised an unexpected error")
        await asyncio.sleep(caldav_push.SYNC_INTERVAL_SECONDS)


def poll_mail_once() -> mailbox.PollResult:
    """One mailbox poll, start to finish. Blocking; call it in a thread."""
    conn = db.connect()
    try:
        return mailbox.sync(conn)
    finally:
        conn.close()


async def _mail_loop() -> None:
    await asyncio.sleep(STARTUP_DELAY_SECONDS * 3)
    while True:
        try:
            await asyncio.to_thread(poll_mail_once)
        except mailbox.MailboxError as exc:
            # Already recorded in sync_state and shown on the status page. A mail
            # provider having a bad afternoon is not a fault of ours, and the loop
            # keeps going.
            log.warning("Mail poll failed: %s", exc)
        except Exception:
            log.exception("Mail poll raised an unexpected error")
        await asyncio.sleep(mailbox.POLL_INTERVAL_SECONDS)


def start(app) -> list[asyncio.Task]:
    """Start background jobs, returning the tasks so shutdown can cancel them."""
    tasks: list[asyncio.Task] = []

    if config.canvas_configured():
        tasks.append(asyncio.create_task(_canvas_loop(), name="canvas-poll"))
        log.info("Canvas polling every %d minutes.", CANVAS_POLL_SECONDS // 60)
    else:
        log.info(
            "CANVAS_ICS_URL is not set, so the Canvas feed will not be polled. "
            "Add it to the secrets file to turn ingestion on — see docs/SECRETS.md."
        )

    if caldav_push.configured():
        tasks.append(asyncio.create_task(_reminder_loop(), name="reminder-sync"))
        log.info(
            "Reminders syncing to Apple every %d minutes.",
            caldav_push.SYNC_INTERVAL_SECONDS // 60,
        )
    else:
        log.info(
            "CALDAV_USERNAME and CALDAV_PASSWORD are not set, so reminders will not "
            "reach your phone. See docs/SETUP.md section 16."
        )

    if mailbox.configured():
        tasks.append(asyncio.create_task(_mail_loop(), name="mail-poll"))
        log.info(
            "Collecting forwarded mail every %d minutes.",
            mailbox.POLL_INTERVAL_SECONDS // 60,
        )
    else:
        log.info(
            "MAIL_IMAP_HOST, MAIL_USERNAME and MAIL_PASSWORD are not all set, so no "
            "mail will be collected. See docs/SETUP.md section 18."
        )

    # No configuration to check: this reads and writes the local database only,
    # and generates nothing until an exam has an estimate.
    tasks.append(asyncio.create_task(_study_session_loop(), name="study-sessions"))

    return tasks


async def stop(tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
