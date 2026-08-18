"""Tests for the Today view and its one-tap actions (SPEC §9, §12).

SPEC §12's criterion for this milestone: "If it takes more than one tap to know
what to do next, this milestone is not done."
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import config, db, migrate
from app.main import app


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    migrate.run(path)
    return path


@pytest.fixture
def client(db_path):
    with TestClient(app) as test_client:
        yield test_client


def iso(days=0, hours=0):
    when = datetime.now(timezone.utc) + timedelta(days=days, hours=hours)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def add(path, title, **kwargs):
    fields = {
        "course_id": None, "type": "other", "status": "not_started",
        "source": "manual", "due_at": None, "est_hours": None,
        "est_hours_remaining": None, "pinned": 0,
    }
    fields.update(kwargs)
    conn = db.connect(path)
    try:
        cur = conn.execute(
            "INSERT INTO assignments (title, course_id, type, status, source, due_at, "
            "est_hours, est_hours_remaining, pinned) VALUES (?,?,?,?,?,?,?,?,?)",
            (title, fields["course_id"], fields["type"], fields["status"],
             fields["source"], fields["due_at"], fields["est_hours"],
             fields["est_hours_remaining"], fields["pinned"]),
        )
        return cur.lastrowid
    finally:
        conn.close()


def field(path, assignment_id, column):
    conn = db.connect(path)
    try:
        return conn.execute(
            f"SELECT {column} AS v FROM assignments WHERE id = ?", (assignment_id,)
        ).fetchone()["v"]
    finally:
        conn.close()


def text_of(response):
    return " ".join(response.text.split())


# --- the default screen -----------------------------------------------------


def test_today_is_the_default_screen(client):
    """SPEC §12 calls the Today view "the default screen"."""
    body = client.get("/").text
    assert "<h1>Today</h1>" in body


def test_the_status_page_moved_but_still_works(client):
    assert "Everything checks out" in client.get("/status").text


def test_the_top_item_is_named_not_merely_first(client, db_path):
    """One tap to know what to do next means the answer must be labelled."""
    add(db_path, "Big paper", due_at=iso(days=3), est_hours_remaining=14.0)
    add(db_path, "Small worksheet", due_at=iso(days=1), est_hours_remaining=0.3)

    body = text_of(client.get("/"))
    assert "Start with this" in body
    # The paper is tighter on slack despite the later deadline.
    assert body.index("Big paper") < body.index("Small worksheet")


def test_every_ranked_item_shows_the_numbers_behind_it(client, db_path):
    """SPEC §9 display rule 2, and rule 1: no bare score anywhere."""
    add(db_path, "Essay", due_at=iso(days=2), est_hours_remaining=3.0)

    body = text_of(client.get("/"))
    assert "3h of work left" in body
    assert "h free before then" in body
    assert "to spare" in body
    # Rule 1: never show a rank number or a score.
    assert "Priority:" not in body
    assert "score" not in body.lower()


def test_the_ordering_is_explained_in_a_sentence(client, db_path):
    """SPEC §9 display rule 3."""
    add(db_path, "Essay", due_at=iso(days=2), est_hours_remaining=3.0)
    body = text_of(client.get("/"))
    assert "Ordered by how much spare time is left before each deadline" in body
    assert "four productive hours a day" in body


def test_being_behind_is_stated_plainly(client, db_path):
    add(db_path, "Doomed", due_at=iso(days=1), est_hours_remaining=40.0)
    assert "short of the time needed" in text_of(client.get("/"))


# --- estimates --------------------------------------------------------------


def test_items_without_an_estimate_are_asked_for_not_guessed(client, db_path):
    add(db_path, "Unknown effort", due_at=iso(days=2))
    body = text_of(client.get("/"))
    assert "need an estimate before they can be ranked" in body
    assert "Unknown effort" in body


def test_one_tap_sets_an_estimate(client, db_path):
    assignment_id = add(db_path, "Reading", due_at=iso(days=2))

    response = client.post(
        f"/assignments/{assignment_id}/estimate", data={"hours": "2.0"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    assert field(db_path, assignment_id, "est_hours") == 2.0
    assert field(db_path, assignment_id, "est_hours_remaining") == 2.0
    # And now it ranks.
    assert "2h of work left" in text_of(client.get("/"))


def test_an_absurd_estimate_is_refused(client, db_path):
    assignment_id = add(db_path, "Reading", due_at=iso(days=2))
    assert client.post(f"/assignments/{assignment_id}/estimate",
                       data={"hours": "0"}).status_code == 400
    assert client.post(f"/assignments/{assignment_id}/estimate",
                       data={"hours": "500"}).status_code == 400
    assert field(db_path, assignment_id, "est_hours") is None


def test_setting_an_estimate_is_recorded(client, db_path):
    assignment_id = add(db_path, "Reading", due_at=iso(days=2))
    client.post(f"/assignments/{assignment_id}/estimate", data={"hours": "1.0"})

    conn = db.connect(db_path)
    try:
        actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log")]
    finally:
        conn.close()
    assert "estimate" in actions


# --- one-tap status ---------------------------------------------------------


def test_one_tap_marks_something_done(client, db_path):
    """SPEC §12: "mark something done in one tap"."""
    assignment_id = add(db_path, "Quiz", due_at=iso(days=1), est_hours_remaining=1.0)

    response = client.post(f"/assignments/{assignment_id}/status",
                           data={"value": "submitted"}, follow_redirects=False)
    assert response.status_code == 303
    assert field(db_path, assignment_id, "status") == "submitted"
    # Finished work stops reserving time and leaves the ranking.
    assert field(db_path, assignment_id, "est_hours_remaining") == 0
    assert "Quiz" not in text_of(client.get("/"))


def test_one_tap_starts_something(client, db_path):
    assignment_id = add(db_path, "Lab", due_at=iso(days=2), est_hours_remaining=2.0)
    client.post(f"/assignments/{assignment_id}/status", data={"value": "in_progress"})
    assert field(db_path, assignment_id, "status") == "in_progress"
    assert "in progress" in text_of(client.get("/"))


def test_status_changes_are_recorded(client, db_path):
    assignment_id = add(db_path, "Lab", due_at=iso(days=2), est_hours_remaining=2.0)
    client.post(f"/assignments/{assignment_id}/status", data={"value": "in_progress"})

    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT detail_json FROM audit_log WHERE action = 'status_change'"
        ).fetchone()
    finally:
        conn.close()
    assert "not_started" in row["detail_json"]
    assert "in_progress" in row["detail_json"]


def test_an_unknown_status_is_refused(client, db_path):
    assignment_id = add(db_path, "Lab", due_at=iso(days=2))
    assert client.post(f"/assignments/{assignment_id}/status",
                       data={"value": "invented"}).status_code == 400


def test_acting_on_something_that_does_not_exist_is_a_404(client):
    assert client.post("/assignments/9999/status",
                       data={"value": "submitted"}).status_code == 404


# --- pinning ----------------------------------------------------------------


def test_pinning_forces_an_item_to_the_top(client, db_path):
    """SPEC §9: the manual override always wins."""
    add(db_path, "Urgent thing", due_at=iso(days=1), est_hours_remaining=3.5)
    pinned = add(db_path, "My priority", due_at=iso(days=20), est_hours_remaining=0.5)

    client.post(f"/assignments/{pinned}/pin")

    body = text_of(client.get("/"))
    assert body.index("My priority") < body.index("Urgent thing")
    assert "pinned" in body


# --- quick capture ----------------------------------------------------------


def test_quick_capture_takes_anything(client, db_path):
    """SPEC §9: "Dump anything, triage later." Entry friction kills these systems."""
    response = client.post("/capture", data={"text": "ask Dr Reyes about the recital"},
                           follow_redirects=False)
    assert response.status_code == 303

    body = text_of(client.get("/"))
    assert "ask Dr Reyes about the recital" in body
    assert "Needs a date" in body


def test_capturing_nothing_does_nothing(client, db_path):
    client.post("/capture", data={"text": "   "})
    conn = db.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) n FROM assignments").fetchone()["n"] == 0
    finally:
        conn.close()


def test_a_captured_item_is_not_ranked_without_a_date(client, db_path):
    client.post("/capture", data={"text": "vague idea"})
    body = text_of(client.get("/"))
    # It appears, but under the section that says why it cannot be placed.
    assert "Needs a date" in body
    assert "Start with this" not in body


# --- course renaming --------------------------------------------------------


def _course(path, name, code, needs_naming=1):
    conn = db.connect(path)
    try:
        conn.execute(
            "INSERT INTO terms (id, name, start_date, end_date) "
            "VALUES (1,'FA26','2026-08-01','2026-12-15')"
        )
        cur = conn.execute(
            "INSERT INTO courses (term_id, name, code, needs_naming) VALUES (1,?,?,?)",
            (name, code, needs_naming),
        )
        return cur.lastrowid
    finally:
        conn.close()


def test_courses_named_after_a_code_are_offered_for_renaming(client, db_path):
    _course(db_path, "FA26-BL-MATH-M211-2050", "MATH-M211")
    body = text_of(client.get("/"))
    assert "Courses still named after a code" in body
    assert "FA26-BL-MATH-M211-2050" in body


def test_renaming_a_course_clears_the_flag(client, db_path):
    course_id = _course(db_path, "FA26-BL-MATH-M211-2050", "MATH-M211")

    client.post(f"/courses/{course_id}/name", data={"name": "Calculus I"})

    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT name, needs_naming FROM courses WHERE id = ?", (course_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["name"] == "Calculus I"
    assert row["needs_naming"] == 0
    assert "Courses still named after a code" not in text_of(client.get("/"))


def test_renaming_to_nothing_is_ignored(client, db_path):
    course_id = _course(db_path, "FA26-BL-MATH-M211-2050", "MATH-M211")
    client.post(f"/courses/{course_id}/name", data={"name": "  "})
    conn = db.connect(db_path)
    try:
        assert conn.execute(
            "SELECT needs_naming n FROM courses WHERE id = ?", (course_id,)
        ).fetchone()["n"] == 1
    finally:
        conn.close()


# --- sections ---------------------------------------------------------------


def test_overdue_work_is_separated_out(client, db_path):
    add(db_path, "Missed it", due_at=iso(days=-2), est_hours_remaining=1.0)
    body = text_of(client.get("/"))
    assert "Past due" in body
    assert "Missed it" in body


def test_distant_work_does_not_crowd_the_screen(client, db_path):
    add(db_path, "Next week", due_at=iso(days=5), est_hours_remaining=2.0)
    add(db_path, "Miles away", due_at=iso(days=60), est_hours_remaining=2.0)
    body = text_of(client.get("/"))
    assert "Further out" in body
    assert body.index("Next week") < body.index("Miles away")


def test_an_empty_dashboard_says_so(client):
    assert "Nothing due in the next" in text_of(client.get("/"))


def test_the_feeds_limits_are_restated_here_too(client):
    """SPEC §6.3 wants this where it will actually be read."""
    assert "only knows about work with a due date" in text_of(client.get("/"))
