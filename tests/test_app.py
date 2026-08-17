"""Tests for the M0 web app: the status page, /healthz, and the PWA assets."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    response = client.get("/")
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
    assert "Nothing runs on a schedule yet" in client.get("/").text


def test_status_page_shows_a_failing_job(client, db_path):
    _sync_row(
        db_path,
        "canvas_ics",
        success_hours_ago=9,
        failures=4,
        error="HTTPSConnectionPool: Read timed out",
    )
    body = client.get("/").text
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
