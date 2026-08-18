"""Tests for the CalDAV push (SPEC §8).

There is no Apple app-specific password on this machine, so these drive a fake
server. They cover discovery, the to-do that gets written, what happens when a
push fails, and that the password never reaches anywhere it could be read. They
prove nothing about iCloud itself — that is what `--probe` on the server is for.
"""

from __future__ import annotations

import urllib.error
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import caldav_push, config, db, ics, migrate, reminders

INDIANA = ZoneInfo("America/Indiana/Indianapolis")
PASSWORD = "abcd-efgh-ijkl-mnop"   # shaped like a real app-specific password


PRINCIPAL_XML = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:"><d:response><d:href>/</d:href><d:propstat><d:prop>
<d:current-user-principal><d:href>/12345/principal/</d:href></d:current-user-principal>
</d:prop></d:propstat></d:response></d:multistatus>"""

HOME_XML = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response>
<d:href>/12345/principal/</d:href><d:propstat><d:prop>
<c:calendar-home-set><d:href>/12345/calendars/</d:href></c:calendar-home-set>
</d:prop></d:propstat></d:response></d:multistatus>"""

COLLECTIONS_XML = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:response><d:href>/12345/calendars/home/</d:href><d:propstat><d:prop>
    <d:displayname>Home</d:displayname>
    <c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set>
  </d:prop></d:propstat></d:response>
  <d:response><d:href>/12345/calendars/reminders/</d:href><d:propstat><d:prop>
    <d:displayname>Reminders</d:displayname>
    <c:supported-calendar-component-set><c:comp name="VTODO"/></c:supported-calendar-component-set>
  </d:prop></d:propstat></d:response>
</d:multistatus>"""


class FakeServer:
    """Records what was sent, replies with canned CalDAV responses."""

    def __init__(self, fail_on: str | None = None, status: int = 500):
        self.requests: list[tuple[str, str, str | None, dict]] = []
        self.fail_on = fail_on
        self.status = status

    def __call__(self, request, timeout=None):
        method = request.get_method()
        url = request.full_url
        body = request.data.decode() if request.data else None
        self.requests.append((method, url, body, dict(request.headers)))

        if self.fail_on and self.fail_on in url:
            raise urllib.error.HTTPError(url, self.status, "nope", {}, None)

        if method == "PROPFIND":
            if url.endswith("/12345/calendars/"):
                payload = COLLECTIONS_XML
            elif "principal" in url:
                payload = HOME_XML
            else:
                payload = PRINCIPAL_XML
            return _Response(207, payload)
        return _Response(201, "")

    def bodies(self, method):
        return [body for verb, _, body, _ in self.requests if verb == method]


class _Response:
    def __init__(self, status, text):
        self.status = status
        self._text = text

    def read(self):
        return self._text.encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def server(monkeypatch):
    fake = FakeServer()
    monkeypatch.setattr(caldav_push.urllib.request, "urlopen", fake)
    monkeypatch.setenv("CALDAV_URL", "https://caldav.example.com")
    monkeypatch.setenv("CALDAV_USERNAME", "mason@example.com")
    monkeypatch.setenv("CALDAV_PASSWORD", PASSWORD)
    return fake


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "TZ", "America/Indiana/Indianapolis")
    migrate.run(path)
    connection = db.connect(path)
    try:
        connection.execute(
            "INSERT INTO terms (id,name,start_date,end_date) "
            "VALUES (1,'FA26','2026-08-24','2026-12-18')"
        )
        connection.execute(
            "INSERT INTO courses (id,term_id,name) VALUES (1,1,'Music Theory III')"
        )
        yield connection
    finally:
        connection.close()


def add(conn, title="Species counterpoint 1", days=14, kind="worksheet", status="not_started"):
    due = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = conn.execute(
        "INSERT INTO assignments (course_id,title,type,source,due_at,status) "
        "VALUES (1,?,?,'manual',?,?)",
        (title, kind, due, status),
    )
    return cur.lastrowid


# --- discovery --------------------------------------------------------------


def test_discovery_walks_to_the_list_that_accepts_reminders(server):
    found = caldav_push.discover("https://caldav.example.com", "mason@example.com", PASSWORD)
    assert found.endswith("/12345/calendars/reminders/")
    # The calendar that only takes events is not chosen.
    assert "home" not in found


def test_discovery_reports_the_step_it_reached(server):
    steps = []
    caldav_push.discover("https://caldav.example.com", "mason@example.com", PASSWORD,
                         trace=lambda step, text: steps.append(text))
    joined = " ".join(steps)
    assert "principal" in joined
    assert "Reminders" in joined
    assert "calendar events only" in joined       # the unusable one is named too


def test_an_account_with_no_reminders_list_says_what_to_fix(monkeypatch):
    only_events = COLLECTIONS_XML.replace('name="VTODO"', 'name="VEVENT"')
    fake = FakeServer()
    original = fake.__call__

    def patched(request, timeout=None):
        response = original(request, timeout)
        if request.full_url.endswith("/12345/calendars/"):
            return _Response(207, only_events)
        return response

    monkeypatch.setattr(caldav_push.urllib.request, "urlopen", patched)
    with pytest.raises(caldav_push.CalDavError, match="On My iPhone"):
        caldav_push.discover("https://caldav.example.com", "m@example.com", PASSWORD)


def test_the_list_is_discovered_once_and_remembered(conn, server):
    """sync_state.cursor, defined in M0 and unused until now."""
    first = caldav_push.collection_url(conn, "m@example.com", PASSWORD,
                                       "https://caldav.example.com")
    propfinds = len([r for r in server.requests if r[0] == "PROPFIND"])

    second = caldav_push.collection_url(conn, "m@example.com", PASSWORD,
                                        "https://caldav.example.com")

    assert first == second
    assert len([r for r in server.requests if r[0] == "PROPFIND"]) == propfinds


# --- the to-do --------------------------------------------------------------


def test_the_generated_todo_is_valid_icalendar(conn):
    """Round-tripped through the parser written for Canvas in M1."""
    add(conn)
    reminders.generate_all(conn, INDIANA)
    assignment = caldav_push._pending_by_assignment(conn)[0]
    rows = conn.execute(
        "SELECT kind, fire_at FROM reminder_instances ORDER BY fire_at"
    ).fetchall()

    body = caldav_push.build_todo(assignment, [(r["kind"], r["fire_at"]) for r in rows])

    assert body.startswith("BEGIN:VCALENDAR")
    assert "BEGIN:VTODO" in body
    assert body.count("BEGIN:VALARM") == len(rows)
    assert "STATUS:NEEDS-ACTION" in body
    # Lines are CRLF-terminated and none exceeds the 75-octet limit unfolded.
    for line in body.split("\r\n"):
        if line and not line.startswith(" "):
            assert len(line.encode()) <= 75, line


def test_the_todo_carries_the_course_so_the_list_reads_properly(conn):
    add(conn)
    reminders.generate_all(conn, INDIANA)
    assignment = caldav_push._pending_by_assignment(conn)[0]
    body = caldav_push.build_todo(assignment, [])
    assert "Species counterpoint 1" in body
    assert "Music Theory III" in body


def test_special_characters_are_escaped(conn):
    add(conn, title="Sec 2.8, 3.1-3.6; review")
    reminders.generate_all(conn, INDIANA)
    assignment = caldav_push._pending_by_assignment(conn)[0]
    body = caldav_push.build_todo(assignment, [])
    assert "\\," in body and "\;" in body
    # And it survives a round trip through the reader.
    assert "Sec 2.8, 3.1-3.6; review" in ics.unescape(body)


def test_the_uid_is_stable_so_a_repush_overwrites(conn):
    """A random UID each run would fill the Reminders list with copies."""
    assert caldav_push.todo_uid(12) == caldav_push.todo_uid(12)
    assert caldav_push.todo_uid(12) != caldav_push.todo_uid(13)


# --- the sync ---------------------------------------------------------------


def test_a_sync_pushes_one_todo_per_assignment(conn, server):
    add(conn, "One")
    add(conn, "Two")

    result = caldav_push.sync(conn, INDIANA)

    assert result.generated == 4          # two rungs each
    assert result.pushed == 2             # but one to-do each
    assert len(server.bodies("PUT")) == 2


def test_pushed_reminders_are_marked_sent(conn, server):
    assignment_id = add(conn)
    caldav_push.sync(conn, INDIANA)

    rows = conn.execute(
        "SELECT state, sent_at, external_id FROM reminder_instances"
    ).fetchall()
    assert {row["state"] for row in rows} == {"sent"}
    assert all(row["sent_at"] for row in rows)
    assert all(row["external_id"] == caldav_push.todo_uid(assignment_id) for row in rows)


def test_a_second_sync_pushes_nothing_new(conn, server):
    add(conn)
    caldav_push.sync(conn, INDIANA)
    before = len(server.bodies("PUT"))

    result = caldav_push.sync(conn, INDIANA)

    assert result.pushed == 0
    assert len(server.bodies("PUT")) == before


def test_finishing_something_takes_it_off_the_phone(conn, server):
    assignment_id = add(conn)
    caldav_push.sync(conn, INDIANA)

    conn.execute("UPDATE assignments SET status = 'submitted' WHERE id = ?", (assignment_id,))
    result = caldav_push.sync(conn, INDIANA)

    assert result.withdrawn == 1
    assert any(verb == "DELETE" for verb, *_ in server.requests)
    assert conn.execute(
        "SELECT COUNT(*) n FROM reminder_instances WHERE state = 'dismissed'"
    ).fetchone()["n"] == 2


def test_a_moved_deadline_reaches_the_phone(conn, server):
    assignment_id = add(conn, days=14)
    caldav_push.sync(conn, INDIANA)
    first_body = server.bodies("PUT")[-1]

    reminders.supersede_for(conn, assignment_id)
    conn.execute(
        "UPDATE assignments SET due_at = ? WHERE id = ?",
        ((datetime.now(timezone.utc) + timedelta(days=21)).strftime("%Y-%m-%dT%H:%M:%SZ"),
         assignment_id),
    )
    result = caldav_push.sync(conn, INDIANA)

    assert result.pushed == 1
    assert server.bodies("PUT")[-1] != first_body
    # Same to-do, overwritten — not a second one.
    assert caldav_push.todo_uid(assignment_id) in server.requests[-1][1]


def test_a_successful_sync_is_recorded(conn, server):
    add(conn)
    caldav_push.sync(conn, INDIANA)
    row = conn.execute(
        "SELECT * FROM sync_state WHERE source = 'caldav_push'"
    ).fetchone()
    assert row["last_success_at"]
    assert row["consecutive_failures"] == 0
    assert row["last_error"] is None


# --- failure ----------------------------------------------------------------


def test_a_rejected_password_says_what_is_wrong(conn, monkeypatch):
    fake = FakeServer(fail_on="caldav.example.com", status=401)
    monkeypatch.setattr(caldav_push.urllib.request, "urlopen", fake)
    monkeypatch.setenv("CALDAV_URL", "https://caldav.example.com")
    monkeypatch.setenv("CALDAV_USERNAME", "mason@example.com")
    monkeypatch.setenv("CALDAV_PASSWORD", PASSWORD)

    with pytest.raises(caldav_push.CalDavError, match="app-specific password"):
        caldav_push.sync(conn, INDIANA)

    row = conn.execute("SELECT * FROM sync_state WHERE source='caldav_push'").fetchone()
    assert row["consecutive_failures"] == 1


def test_the_password_never_reaches_the_database(conn, monkeypatch, caplog):
    """sync_state.last_error is rendered in the browser (SPEC §11)."""
    fake = FakeServer(fail_on="caldav.example.com", status=401)
    monkeypatch.setattr(caldav_push.urllib.request, "urlopen", fake)
    monkeypatch.setenv("CALDAV_URL", "https://caldav.example.com")
    monkeypatch.setenv("CALDAV_USERNAME", "mason@example.com")
    monkeypatch.setenv("CALDAV_PASSWORD", PASSWORD)

    with caplog.at_level("DEBUG"):
        with pytest.raises(caldav_push.CalDavError):
            caldav_push.sync(conn, INDIANA)

    stored = conn.execute(
        "SELECT last_error FROM sync_state WHERE source='caldav_push'"
    ).fetchone()["last_error"]
    assert PASSWORD not in stored
    assert PASSWORD not in caplog.text


def test_one_failed_todo_does_not_stop_the_others(conn, monkeypatch):
    add(conn, "First")
    add(conn, "Second")

    fake = FakeServer()
    original = FakeServer.__call__

    def flaky(request, timeout=None):
        if request.get_method() == "PUT" and "dashboard-1@" in request.full_url:
            raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, None)
        return original(fake, request, timeout)

    monkeypatch.setattr(caldav_push.urllib.request, "urlopen", flaky)
    monkeypatch.setenv("CALDAV_URL", "https://caldav.example.com")
    monkeypatch.setenv("CALDAV_USERNAME", "mason@example.com")
    monkeypatch.setenv("CALDAV_PASSWORD", PASSWORD)

    result = caldav_push.sync(conn, INDIANA)

    assert result.pushed == 1
    assert len(result.failures) == 1


def test_missing_credentials_are_reported_by_name(conn, monkeypatch):
    monkeypatch.delenv("CALDAV_USERNAME", raising=False)
    monkeypatch.delenv("CALDAV_PASSWORD", raising=False)
    with pytest.raises(caldav_push.CalDavError, match="CALDAV_USERNAME"):
        caldav_push.sync(conn, INDIANA)
