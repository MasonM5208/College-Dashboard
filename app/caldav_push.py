"""Keeping Apple Reminders in step with the dashboard.

SPEC §8 puts every time-based nag on this channel, and explains why it is not web
push: iOS needs the PWA installed, a permission prompt from a user tap, VAPID keys
and a service worker, and **drops the push registration if the app goes unopened
for several weeks** — which is exactly what happens during the stretch when the
reminders matter most. Apple Reminders has none of that, and iOS owns delivery,
snoozing, the lock screen and the Watch.

Each assignment becomes one to-do carrying all of its alert times, so the
scheduled job is not "fire a reminder at the right moment" — it is "keep Apple's
copy in step with ours". That is the better failure mode by a distance: if this
server is down at 7am on a Saturday, the alert still fires, because it was pushed
days ago and lives on the phone.

Written against the standard library. The `caldav` package pulls in seven
dependencies including a C extension, to do three verbs — PROPFIND to find the
list, PUT to write a to-do, DELETE to remove one.

**Completion does not flow back.** SPEC §8 defers that: ticking a reminder off in
iOS will not mark it done here, because reading state back needs a CalDAV poll
loop. The dashboard is the source of truth for status.
"""

from __future__ import annotations

import base64
import logging
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app import config, reminders

log = logging.getLogger("caldav")

SOURCE = "caldav_push"
TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"
ICAL_FMT = "%Y%m%dT%H%M%SZ"
TIMEOUT = 30.0

# Apple's CalDAV entry point. Overridable for anyone not on iCloud.
DEFAULT_URL = "https://caldav.icloud.com"

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
NS = {"d": DAV, "c": CALDAV}

# Long enough that a burst of edits settles between pushes, short enough that a
# deadline changed in the morning reaches the phone the same morning.
SYNC_INTERVAL_SECONDS = 15 * 60

# Stamped onto the cached collection address. Bumping it discards addresses chosen
# by an older discovery rule instead of trusting them.
CURSOR_VERSION = "v2:"


class CalDavError(RuntimeError):
    """A push failed. The message is always safe to display and to store."""


@dataclass
class SyncResult:
    pushed: int = 0
    withdrawn: int = 0
    generated: int = 0
    failures: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.generated} reminder(s) built, {self.pushed} to-do(s) pushed, "
            f"{self.withdrawn} withdrawn"
        )


# --- talking to the server --------------------------------------------------


def _auth_header(username: str, password: str) -> str:
    raw = f"{username}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _request(
    method: str,
    url: str,
    username: str,
    password: str,
    body: str | None = None,
    depth: str | None = None,
    content_type: str | None = None,
) -> tuple[int, str]:
    """One CalDAV request.

    Every failure is re-raised as a CalDavError carrying a fixed description. The
    original exception is suppressed: an authentication failure will quote the
    request, `sync_state.last_error` is rendered in the browser, and SPEC §11
    forbids secrets in error messages.
    """
    request = urllib.request.Request(
        url,
        data=body.encode("utf-8") if body else None,
        method=method,
    )
    request.add_header("Authorization", _auth_header(username, password))
    request.add_header("User-Agent", "SemesterDashboard/1.0")
    if depth is not None:
        request.add_header("Depth", depth)
    if content_type:
        request.add_header("Content-Type", content_type)

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and method == "DELETE":
            # Already gone. Withdrawing something twice is not a failure.
            return 404, ""

        # Apple explains a rejected write in the response body, naming the
        # precondition that failed. That is the difference between "HTTP 400" and
        # a fixable sentence, and the body is server-generated XML with no
        # credentials in it — unlike the exception's own string, which quotes the
        # request.
        detail = ""
        try:
            body = exc.read().decode("utf-8", errors="replace").strip()
            if body:
                detail = " " + " ".join(body.split())[:400]
        except Exception:
            pass
        raise CalDavError(_http_message(exc.code, method) + detail) from None
    except urllib.error.URLError:
        raise CalDavError(
            "Could not reach the reminders server. The server may be offline, or "
            "its network connection may be down."
        ) from None
    except TimeoutError:
        raise CalDavError(
            f"The reminders server did not answer within {int(TIMEOUT)} seconds."
        ) from None


def _http_message(status: int, method: str) -> str:
    if status in (401, 403):
        return (
            f"Apple rejected the sign-in (HTTP {status}). This is almost always the "
            f"app-specific password — a normal Apple ID password will not work here. "
            f"See docs/SECRETS.md."
        )
    if status == 404:
        return f"The reminders list was not found (HTTP {status})."
    if status == 507:
        return "Apple reports the account is out of storage (HTTP 507)."
    if status >= 500:
        return f"Apple had a server problem (HTTP {status}). This usually clears on its own."
    return f"Apple returned an unexpected response to {method} (HTTP {status})."


# --- finding the list -------------------------------------------------------

_PROPFIND_PRINCIPAL = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>"""

_PROPFIND_HOME = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><c:calendar-home-set/></d:prop></d:propfind>"""

_PROPFIND_COLLECTIONS = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop>
    <d:displayname/>
    <d:resourcetype/>
    <c:supported-calendar-component-set/>
  </d:prop>
</d:propfind>"""


# iCloud publishes scheduling collections in the same calendar home as the real
# lists. They advertise component support and then reject every write, which is
# how a discovery that only checks components ends up with 400 on every PUT.
SCHEDULING_COLLECTIONS = ("inbox", "outbox", "notification", "dropbox")


@dataclass
class Collection:
    url: str
    name: str
    accepts_todos: bool
    is_calendar: bool = False
    reason: str = ""

    @property
    def usable(self) -> bool:
        return self.accepts_todos and self.is_calendar and not self.reason


def _absolute(base: str, href: str) -> str:
    return urllib.parse.urljoin(base, href)


def discover(url: str, username: str, password: str, trace=None) -> str:
    """Find the collection that accepts to-dos.

    Four requests, each depending on the last, which is why the probe reports them
    one at a time: knowing discovery reached the calendar home and then found no
    to-do list is a fixable sentence, where "reminders do not work" is not.
    """
    say = trace or (lambda *_: None)

    say("step 1", f"asking {url} who we are signed in as")
    _, body = _request("PROPFIND", url, username, password,
                       _PROPFIND_PRINCIPAL, depth="0",
                       content_type="application/xml; charset=utf-8")
    node = ET.fromstring(body).find(".//d:current-user-principal/d:href", NS)
    if node is None or not node.text:
        raise CalDavError(
            "Signed in, but the server did not say which account this is. That "
            "usually means the address is not a CalDAV endpoint."
        )
    principal = _absolute(url, node.text.strip())
    say("step 2", f"account is {principal}")

    _, body = _request("PROPFIND", principal, username, password,
                       _PROPFIND_HOME, depth="0",
                       content_type="application/xml; charset=utf-8")
    node = ET.fromstring(body).find(".//c:calendar-home-set/d:href", NS)
    if node is None or not node.text:
        raise CalDavError("The account has no calendar home, so there is nowhere to write.")
    home = _absolute(principal, node.text.strip())
    say("step 3", f"calendar home is {home}")

    _, body = _request("PROPFIND", home, username, password,
                       _PROPFIND_COLLECTIONS, depth="1",
                       content_type="application/xml; charset=utf-8")

    collections = []
    for response in ET.fromstring(body).findall("d:response", NS):
        href = response.find("d:href", NS)
        if href is None or not href.text:
            continue
        name = response.find(".//d:displayname", NS)
        components = [
            comp.get("name")
            for comp in response.findall(".//c:supported-calendar-component-set/c:comp", NS)
        ]
        if not components:
            continue

        path = href.text.strip()
        # A real list is a CalDAV calendar. Anything else advertising component
        # support is a scheduling endpoint and will refuse to be written to.
        is_calendar = response.find(".//d:resourcetype/c:calendar", NS) is not None
        reason = ""
        if not is_calendar:
            reason = "not a calendar collection"
        elif any(part in path.lower().split("/") for part in SCHEDULING_COLLECTIONS):
            reason = "a scheduling collection, not a list"

        collections.append(
            Collection(
                url=_absolute(home, path),
                name=(name.text if name is not None and name.text else "(unnamed)"),
                accepts_todos="VTODO" in components,
                is_calendar=is_calendar,
                reason=reason,
            )
        )

    say("step 4", f"found {len(collections)} collection(s):")
    for collection in collections:
        if collection.usable:
            mark = "accepts reminders"
        elif collection.reason:
            mark = f"skipped — {collection.reason}"
        else:
            mark = "calendar events only"
        say("       ", f"{collection.name} — {mark}")

    usable = [c for c in collections if c.usable]
    if not usable:
        raise CalDavError(
            "The account has calendars but none of them is a reminders list that can "
            "be written to. In the Reminders app on your iPhone, make sure at least "
            "one list is stored in iCloud rather than 'On My iPhone'."
        )

    # Several lists can qualify. Prefer the one actually called Reminders, so the
    # to-dos land where he would look for them rather than in whichever came first.
    chosen = next(
        (c for c in usable if "reminder" in c.name.lower()), usable[0]
    )
    say("step 5", f"will write to: {chosen.name}")
    return chosen.url


def collection_url(conn: sqlite3.Connection, username: str, password: str, url: str) -> str:
    """The list to write to, discovered once and remembered.

    Cached in sync_state.cursor — a column SPEC §5 defined in M0 and nothing has
    used until now. Rediscovering on every run would be four extra requests every
    fifteen minutes for an answer that never changes.
    """
    # The stored value carries the version of the rule that chose it. When that
    # rule changes — as it did after discovery started rejecting scheduling
    # collections — an address cached under the old one is silently wrong, and
    # re-discovering is cheaper than asking anyone to clear it by hand.
    row = conn.execute(
        "SELECT cursor FROM sync_state WHERE source = ?", (SOURCE,)
    ).fetchone()
    if row and row["cursor"] and row["cursor"].startswith(CURSOR_VERSION):
        return row["cursor"][len(CURSOR_VERSION):]

    found = discover(url, username, password)
    conn.execute(
        "INSERT INTO sync_state (source, cursor) VALUES (?, ?) "
        "ON CONFLICT(source) DO UPDATE SET cursor = excluded.cursor",
        (SOURCE, CURSOR_VERSION + found),
    )
    return found


# --- writing the to-do ------------------------------------------------------


def _escape(text: str) -> str:
    """Escape a value for iCalendar. The inverse of app/ics.py's unescape."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """Wrap at 75 octets, continuing with a leading space (RFC 5545).

    The same folding app/ics.py has to undo when reading Canvas. Emitting an
    over-long line is the kind of thing a strict server rejects and a lenient one
    accepts, which is the worst combination to debug.
    """
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line

    chunks, current = [], b""
    for char in line:
        char_bytes = char.encode("utf-8")
        limit = 75 if not chunks else 74
        if len(current) + len(char_bytes) > limit:
            chunks.append(current.decode("utf-8"))
            current = b""
        current += char_bytes
    if current:
        chunks.append(current.decode("utf-8"))
    return "\r\n ".join(chunks)


def todo_uid(assignment_id: int) -> str:
    """A stable name for this assignment's to-do.

    Derived from the id rather than randomly generated, so re-pushing overwrites
    the existing to-do. A random UID each time would fill his Reminders list with
    copies of the same assignment, which is the failure that would make him turn
    the whole thing off.
    """
    return f"dashboard-{assignment_id}@semester-dashboard"


def build_todo(assignment, fire_times: list[tuple[str, str]], now: datetime | None = None) -> str:
    """One VTODO, with one VALARM per rung of the ladder."""
    now = now or datetime.now(timezone.utc)
    due = reminders._parse(assignment["due_at"])

    title = assignment["title"]
    if assignment["course_name"]:
        title = f"{title} · {assignment['course_name']}"

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Semester Dashboard//EN",
        "CALSCALE:GREGORIAN",
        "BEGIN:VTODO",
        f"UID:{todo_uid(assignment['id'])}",
        f"DTSTAMP:{now.strftime(ICAL_FMT)}",
        f"SUMMARY:{_escape(title)}",
        "STATUS:NEEDS-ACTION",
    ]
    if due:
        lines.append(f"DUE:{due.strftime(ICAL_FMT)}")

    for kind, fire_at in fire_times:
        moment = reminders._parse(fire_at)
        if moment is None:
            continue
        label = "Time to start" if kind == "start_by" else "Due soon"
        lines += [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{_escape(f'{label}: {title}')}",
            f"TRIGGER;VALUE=DATE-TIME:{moment.strftime(ICAL_FMT)}",
            "END:VALARM",
        ]

    lines += ["END:VTODO", "END:VCALENDAR"]
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def put_todo(collection: str, uid: str, body: str, username: str, password: str) -> None:
    _request(
        "PUT",
        urllib.parse.urljoin(collection, f"{uid}.ics"),
        username,
        password,
        body=body,
        content_type="text/calendar; charset=utf-8",
    )


def delete_todo(collection: str, uid: str, username: str, password: str) -> None:
    _request("DELETE", urllib.parse.urljoin(collection, f"{uid}.ics"), username, password)


# --- the job ----------------------------------------------------------------


def configured() -> bool:
    return all(
        __import__("os").environ.get(name, "").strip()
        for name in ("CALDAV_USERNAME", "CALDAV_PASSWORD")
    )


def _credentials() -> tuple[str, str, str]:
    import os

    url = os.environ.get("CALDAV_URL", "").strip() or DEFAULT_URL
    try:
        username = config.require("CALDAV_USERNAME")
        password = config.require("CALDAV_PASSWORD")
    except config.MissingSetting as exc:
        raise CalDavError(str(exc)) from None
    return url, username, password


def _now() -> str:
    return datetime.now(timezone.utc).strftime(TIMESTAMP_FMT)


def record_success(conn: sqlite3.Connection, detail: str) -> None:
    now = _now()
    conn.execute(
        "INSERT INTO sync_state (source, last_attempt_at, last_success_at, last_error, "
        "consecutive_failures) VALUES (?,?,?,NULL,0) "
        "ON CONFLICT(source) DO UPDATE SET last_attempt_at = excluded.last_attempt_at, "
        "last_success_at = excluded.last_success_at, last_error = NULL, "
        "consecutive_failures = 0",
        (SOURCE, now, now),
    )


def record_failure(conn: sqlite3.Connection, message: str) -> None:
    conn.execute(
        "INSERT INTO sync_state (source, last_attempt_at, last_error, consecutive_failures) "
        "VALUES (?,?,?,1) "
        "ON CONFLICT(source) DO UPDATE SET last_attempt_at = excluded.last_attempt_at, "
        "last_error = excluded.last_error, "
        "consecutive_failures = sync_state.consecutive_failures + 1",
        (SOURCE, _now(), message),
    )


def _pending_by_assignment(conn: sqlite3.Connection):
    """Assignments with reminders waiting to reach the phone."""
    return conn.execute(
        """
        SELECT a.id, a.title, a.due_at, c.name AS course_name
        FROM assignments a
        LEFT JOIN courses c ON c.id = a.course_id
        WHERE EXISTS (
            SELECT 1 FROM reminder_instances r
            WHERE r.assignment_id = a.id AND r.state = 'pending'
        )
        ORDER BY a.due_at
        """
    ).fetchall()


def _to_withdraw(conn: sqlite3.Connection):
    """Work that is finished but whose to-do is still on the phone."""
    return conn.execute(
        f"""
        SELECT DISTINCT a.id, a.title
        FROM assignments a
        JOIN reminder_instances r ON r.assignment_id = a.id
        WHERE r.state = 'sent'
          AND (a.status IN ({','.join('?' * len(reminders.DONE_STATUSES))})
               OR a.feed_missing_since IS NOT NULL)
        """,
        reminders.DONE_STATUSES,
    ).fetchall()


def sync(conn: sqlite3.Connection, zone: ZoneInfo | None = None) -> SyncResult:
    """Build any missing ladders, then make Apple's copy match ours."""
    zone = zone or ZoneInfo(config.TZ)
    result = SyncResult()

    try:
        url, username, password = _credentials()
    except CalDavError as exc:
        record_failure(conn, "Reminders are not configured.")
        raise

    try:
        result.generated = reminders.generate_all(conn, zone)
        collection = collection_url(conn, username, password, url)
    except CalDavError as exc:
        record_failure(conn, str(exc))
        log.warning("Reminder sync failed during discovery: %s", exc)
        raise

    for assignment in _pending_by_assignment(conn):
        rows = conn.execute(
            "SELECT id, kind, fire_at FROM reminder_instances "
            "WHERE assignment_id = ? AND state IN ('pending','sent') ORDER BY fire_at",
            (assignment["id"],),
        ).fetchall()
        uid = todo_uid(assignment["id"])

        try:
            put_todo(
                collection,
                uid,
                build_todo(assignment, [(r["kind"], r["fire_at"]) for r in rows]),
                username,
                password,
            )
        except CalDavError as exc:
            result.failures.append(str(exc))
            log.warning("Could not push reminders for %r: %s", assignment["title"], exc)
            continue

        conn.execute(
            "UPDATE reminder_instances SET state = 'sent', sent_at = ?, external_id = ? "
            "WHERE assignment_id = ? AND state = 'pending'",
            (_now(), uid, assignment["id"]),
        )
        result.pushed += 1

    for assignment in _to_withdraw(conn):
        try:
            delete_todo(collection, todo_uid(assignment["id"]), username, password)
        except CalDavError as exc:
            result.failures.append(str(exc))
            continue
        conn.execute(
            "UPDATE reminder_instances SET state = 'dismissed' "
            "WHERE assignment_id = ? AND state = 'sent'",
            (assignment["id"],),
        )
        result.withdrawn += 1

    if result.failures:
        record_failure(conn, f"{len(result.failures)} to-do(s) could not be written.")
    else:
        record_success(conn, result.summary())

    log.info("Reminder sync: %s", result.summary())
    return result


# --- the diagnostic ---------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """`python -m app.caldav_push --probe` — check the setup without pushing.

    This exists because the code could not be tested against iCloud before it was
    deployed. It turns "reminders do not work" into a sentence naming the step
    that failed.
    """
    import argparse

    from app import db

    parser = argparse.ArgumentParser(description="Check the reminders connection.")
    parser.add_argument("--probe", action="store_true",
                        help="find the reminders list and stop. Writes nothing.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the to-do that would be sent, and stop.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        url, username, password = _credentials()
    except CalDavError as exc:
        print(f"\nNot configured: {exc}\n")
        return 1

    print(f"\nReminders server: {url}")
    print(f"Signing in as:    {username}\n")

    if args.probe or not args.dry_run:
        try:
            found = discover(url, username, password,
                             trace=lambda step, text: print(f"  {step}  {text}"))
        except CalDavError as exc:
            print(f"\n  FAILED: {exc}\n")
            return 1
        print(f"\n  Reminders will be written to:\n    {found}\n")

    if args.dry_run:
        conn = db.connect()
        try:
            reminders.generate_all(conn, ZoneInfo(config.TZ))
            for assignment in _pending_by_assignment(conn)[:3]:
                rows = conn.execute(
                    "SELECT kind, fire_at FROM reminder_instances "
                    "WHERE assignment_id = ? AND state = 'pending' ORDER BY fire_at",
                    (assignment["id"],),
                ).fetchall()
                print(f"--- {assignment['title']} " + "-" * 40)
                print(build_todo(assignment, [(r["kind"], r["fire_at"]) for r in rows]))
        finally:
            conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
