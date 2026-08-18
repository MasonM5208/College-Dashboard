"""Tests for the web app: the Today view, the status page, /healthz and PWA assets."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import config, db, migrate, status
from app.main import app


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Point the application at a migrated scratch database."""
    path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    migrate.run(path)
    return path


@pytest.fixture
def client(db_path):
    # The context manager is what runs the lifespan startup checks.
    with TestClient(app) as test_client:
        yield test_client


def _sync_row(path, source, *, success_hours_ago=None, failures=0, error=None):
    when = None
    if success_hours_ago is not None:
        when = (
            datetime.now(timezone.utc) - timedelta(hours=success_hours_ago)
        ).strftime(status.TIMESTAMP_FMT)
    conn = db.connect(path)
    try:
        conn.execute(
            "INSERT INTO sync_state (source, last_success_at, last_attempt_at, "
            "last_error, consecutive_failures) VALUES (?,?,?,?,?)",
            (source, when, when, error, failures),
        )
    finally:
        conn.close()


# --- the status page --------------------------------------------------------


def test_status_page_renders(client):
    response = client.get("/status")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]

    body = response.text
    assert "Semester Dashboard" in body
    assert "Everything checks out" in body
    # SPEC §12's hello-world page, reporting the two things M0 had to establish.
    assert "Write-ahead logging" in body
    assert "Full-text search" in body


def test_status_page_links_the_pwa_assets(client):
    body = client.get("/").text
    assert '<link rel="manifest" href="/static/manifest.webmanifest">' in body
    assert '/static/icons/icon-180.png' in body
    assert "navigator.serviceWorker.register('/sw.js')" in body


def test_status_page_says_when_nothing_is_scheduled_yet(client):
    assert "Nothing runs on a schedule yet" in client.get("/status").text


def test_status_page_shows_a_failing_job(client, db_path):
    _sync_row(
        db_path,
        "canvas_ics",
        success_hours_ago=9,
        failures=4,
        error="HTTPSConnectionPool: Read timed out",
    )
    body = client.get("/status").text
    assert "Canvas calendar feed" in body
    assert "4 failure(s) in a row" in body
    # SPEC §4 wants the real error text, not a paraphrase of it.
    assert "Read timed out" in body


# --- /healthz --------------------------------------------------------------


def test_healthz_reports_the_foundation_is_sound(client):
    response = client.get("/healthz")
    assert response.status_code == 200

    payload = response.json()
    assert payload["ok"] is True
    assert payload["checks"] == {
        "journal_mode_wal": True,
        "fts5": True,
        "migrations_up_to_date": True,
    }
    assert payload["schema_version"] == len(migrate.discover())
    assert payload["pending_migrations"] == []


def test_healthz_ignores_a_failing_data_source(client, db_path):
    """A stale Canvas feed is a dashboard warning, not an unhealthy container.

    If sync failures marked the container unhealthy, Docker would restart it every
    time Canvas had an outage, which fixes nothing and hides the real signal.
    """
    _sync_row(db_path, "canvas_ics", success_hours_ago=50, failures=6, error="boom")
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert [s["level"] for s in response.json()["sync_sources"]] == ["failing"]


# --- the assignments view (M1) ---------------------------------------------


def _ingest_fixture(path):
    from pathlib import Path

    from app import canvas, ics

    events = ics.parse_events(
        (Path(__file__).parent / "fixtures" / "canvas_sample.ics").read_text("utf-8")
    )
    conn = db.connect(path)
    try:
        canvas.reconcile(conn, events, ZoneInfo("America/Indiana/Indianapolis"))
    finally:
        conn.close()


def test_assignments_page_is_empty_before_anything_arrives(client):
    body = client.get("/assignments").text
    assert "Nothing has arrived yet" in body


def test_assignments_page_groups_by_course(client, db_path):
    _ingest_fixture(db_path)
    body = client.get("/assignments").text

    assert "Reading response one" in body
    assert "Exam 1 Attempt 1 Sec 1.1 - 2.7" in body
    # The course code stands in as a name until Mason supplies one.
    assert "FA26-XX-STAT-S200-22222" in body
    assert "STAT-S200" in body


def test_assignments_page_shows_what_needs_attention(client, db_path):
    _ingest_fixture(db_path)
    body = client.get("/assignments").text
    assert "could not be matched to a course" in body
    assert "Departmental recital attendance" in body
    assert "still named after a code" in body


def test_assignments_page_states_the_feed_is_not_exhaustive(client, db_path):
    """SPEC §6.3 wants this limitation prominent, not buried."""
    _ingest_fixture(db_path)
    body = client.get("/assignments").text
    assert "Only work that has a due date in Canvas appears here" in body


def test_a_vanished_item_is_shown_not_hidden(client, db_path):
    _ingest_fixture(db_path)
    conn = db.connect(db_path)
    try:
        conn.execute(
            "UPDATE assignments SET feed_missing_since = '2026-09-01T00:00:00Z' "
            "WHERE title = 'Quiz on lab safety'"
        )
    finally:
        conn.close()

    body = client.get("/assignments").text
    assert "disappeared from the feed" in body
    assert "Gone from the Canvas feed since" in body


def test_status_page_links_to_assignments(client, db_path):
    _ingest_fixture(db_path)
    body = client.get("/status").text
    assert 'href="/assignments"' in body
    # Collapse the template's line wrapping before matching on the sentence.
    assert "7 tracked across 3 courses" in " ".join(body.split())


def test_status_page_still_shows_assignments_when_polling_is_off(client, db_path, monkeypatch):
    """Having data and still collecting it are separate facts.

    If the feed address were removed, hiding the assignments already collected
    would make the dashboard look empty rather than stale — the failure SPEC §4
    is most concerned with.
    """
    monkeypatch.delenv("CANVAS_ICS_URL", raising=False)
    _ingest_fixture(db_path)
    body = " ".join(client.get("/status").text.split())
    assert "7 tracked across 3 courses" in body
    assert "nothing new is being collected" in body


def test_status_page_says_when_canvas_is_not_configured(client, monkeypatch):
    monkeypatch.delenv("CANVAS_ICS_URL", raising=False)
    body = client.get("/status").text
    assert "Canvas feed address has not been set" in body


def test_healthz_reports_ingest_counts(client, db_path):
    _ingest_fixture(db_path)
    ingest = client.get("/healthz").json()["ingest"]
    assert ingest["assignments"] == 7
    assert ingest["courses"] == 3
    assert ingest["needs_course"] == 1
    assert ingest["courses_needing_name"] == 3


def test_manual_sync_redirects_even_when_the_feed_fails(client, monkeypatch):
    """A failed poll is recorded and shown, not turned into a 500."""
    from app import canvas

    monkeypatch.setattr(
        canvas, "fetch",
        lambda url, timeout=30.0: (_ for _ in ()).throw(canvas.FeedError("Canvas is down.")),
    )
    monkeypatch.setenv("CANVAS_ICS_URL", "https://example.invalid/secret.ics")

    response = client.post("/sync/canvas", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/assignments"


def test_manual_sync_ingests_a_feed(client, db_path, monkeypatch):
    from pathlib import Path

    from app import canvas

    feed = (Path(__file__).parent / "fixtures" / "canvas_sample.ics").read_text("utf-8")
    monkeypatch.setattr(canvas, "fetch", lambda url, timeout=30.0: feed)
    monkeypatch.setenv("CANVAS_ICS_URL", "https://example.invalid/secret.ics")

    client.post("/sync/canvas", follow_redirects=False)

    assert client.get("/healthz").json()["ingest"]["assignments"] == 7


# --- startup refuses a schema it does not recognise ------------------------


def test_app_refuses_to_serve_when_migrations_are_pending(tmp_path, monkeypatch):
    """An un-migrated database must stop startup, not produce a broken page."""
    path = tmp_path / "empty.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    db.connect(path).close()  # exists, in WAL mode, but has no schema

    with pytest.raises(RuntimeError, match="schema is behind the code"):
        with TestClient(app):
            pass


# --- PWA assets ------------------------------------------------------------


def test_service_worker_is_served_from_the_root(client):
    """Scope: a worker under /static could not control the rest of the site."""
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


def test_service_worker_does_not_cache(client):
    """Guards SPEC §4: a cached page would show stale deadlines as if current."""
    body = client.get("/sw.js").text
    assert "caches.open" not in body
    assert "cache.put" not in body
    assert "cache.match" not in body


def test_manifest_is_valid_and_complete(client):
    response = client.get("/static/manifest.webmanifest")
    assert response.status_code == 200

    manifest = response.json()
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/"
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert "maskable" in {icon["purpose"] for icon in manifest["icons"]}


@pytest.mark.parametrize("size", [180, 192, 512])
def test_icons_are_served_as_png(client, size):
    response = client.get(f"/static/icons/icon-{size}.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")


# --- staleness rules -------------------------------------------------------


def test_a_source_that_never_succeeded_is_not_reported_as_fine(db_path):
    """SPEC §6: a failed poll must never look like "no assignments due"."""
    _sync_row(db_path, "canvas_ics", success_hours_ago=None, failures=1)
    conn = db.connect(db_path)
    try:
        sources = status.sync_sources(conn)
    finally:
        conn.close()
    assert sources[0]["level"] == "stale"
    assert sources[0]["hours_since_success"] is None


def test_canvas_feed_goes_stale_after_three_hours(db_path):
    """SPEC §6: warn once the last successful sync is more than three hours old."""
    _sync_row(db_path, "canvas_ics", success_hours_ago=2)
    conn = db.connect(db_path)
    try:
        assert status.sync_sources(conn)[0]["level"] == "ok"
    finally:
        conn.close()

    conn = db.connect(db_path)
    try:
        conn.execute(
            "UPDATE sync_state SET last_success_at = ? WHERE source = 'canvas_ics'",
            (
                (datetime.now(timezone.utc) - timedelta(hours=4)).strftime(
                    status.TIMESTAMP_FMT
                ),
            ),
        )
        assert status.sync_sources(conn)[0]["level"] == "stale"
    finally:
        conn.close()


def test_a_nightly_backup_is_allowed_to_be_a_day_old(db_path):
    _sync_row(db_path, "backup", success_hours_ago=25)
    conn = db.connect(db_path)
    try:
        assert status.sync_sources(conn)[0]["level"] == "ok"
    finally:
        conn.close()


def test_three_consecutive_failures_escalate(db_path):
    """SPEC §6: "After three, surface a prominent UI warning.\""""
    _sync_row(db_path, "canvas_ics", success_hours_ago=0.2, failures=3)
    conn = db.connect(db_path)
    try:
        assert status.sync_sources(conn)[0]["level"] == "failing"
    finally:
        conn.close()


# --- logging ----------------------------------------------------------------


def test_the_applications_own_log_messages_are_emitted():
    """SPEC §4: every scheduled job logs its outcome.

    uvicorn configures only its own loggers, so without explicit setup the root
    logger stays at WARNING with no handler and every log.info in this package
    vanishes — while Python's last-resort handler still prints warnings. Failures
    would be visible and successes invisible, which makes silence in the log
    ambiguous exactly where it must not be.
    """
    import logging

    assert logging.getLogger().handlers, "no handler on the root logger"
    for name in ("dashboard", "canvas", "scheduler"):
        assert logging.getLogger(name).isEnabledFor(logging.INFO), name


def test_a_successful_canvas_poll_is_logged(db_path, monkeypatch, caplog):
    from pathlib import Path

    from app import canvas

    feed = (Path(__file__).parent / "fixtures" / "canvas_sample.ics").read_text("utf-8")
    monkeypatch.setattr(canvas, "fetch", lambda url, timeout=30.0: feed)

    conn = db.connect(db_path)
    try:
        with caplog.at_level("INFO"):
            canvas.sync(conn, url="https://example.invalid/secret.ics")
    finally:
        conn.close()

    assert "Canvas sync:" in caplog.text
    assert "7 events" in caplog.text
