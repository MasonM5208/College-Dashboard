"""Tests for the M6 screens: overload on Today, the timer, capacity and review.

SPEC §9's display rules are what most of these check. "Never show a bare score or
rank number", "always show the inputs alongside the position", and for overload:
"Do not soften this. Do not hide it behind a toggle. Do not add encouragement."
Those are testable claims about what appears on a page, so they are tested.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import capacity, config, db, migrate
from app.main import app


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
            "INSERT INTO courses (id,term_id,name,code,penalty_pct_per_day) "
            "VALUES (1,1,'Biology 105','BIOL 105',0)"
        )
        yield connection
    finally:
        connection.close()


@pytest.fixture
def client(conn):
    with TestClient(app) as test_client:
        yield test_client


def add(conn, title, hours, days_ahead, **kwargs):
    due = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    fields = {
        "course_id": 1, "title": title, "type": "worksheet", "due_at": due,
        "est_hours": hours, "est_hours_remaining": hours,
        "status": "not_started", "source": "manual", "points_possible": 100,
    }
    fields.update(kwargs)
    columns = ", ".join(fields)
    marks = ", ".join("?" * len(fields))
    cur = conn.execute(
        f"INSERT INTO assignments ({columns}) VALUES ({marks})", list(fields.values())
    )
    return int(cur.lastrowid)


def crush_the_week(conn):
    """Leave one hour a day, so anything substantial overloads."""
    conn.execute("UPDATE capacity_settings SET productive_hours = 1.0")


# --- overload on Today ------------------------------------------------------


def test_a_manageable_week_shows_no_overload_banner(client, conn):
    add(conn, "Small thing", 1.0, 3)
    assert "hours of work" not in client.get("/").text


def test_an_overloaded_week_says_so_in_plain_numbers(client, conn):
    crush_the_week(conn)
    for index in range(4):
        add(conn, f"Item {index}", 6.0, 4)

    body = client.get("/").text

    assert "hours of work" in body
    assert "hours available" in body
    assert "hours short" in body


def test_overload_names_specific_items_with_what_they_cost(client, conn):
    """SPEC §9 step 3: "the specific one or two items to let slide, each with its
    projected grade cost"."""
    crush_the_week(conn)
    add(conn, "Droppable homework", 8.0, 4)
    add(conn, "Also droppable", 8.0, 4)

    body = client.get("/").text

    assert "Droppable homework" in body
    assert "frees" in body
    assert "no late penalty" in body


def test_overload_is_not_behind_a_toggle(client, conn):
    """It is on the default screen or it does not exist."""
    crush_the_week(conn)
    add(conn, "Heavy", 20.0, 3)

    body = client.get("/").text

    assert "hours of work" in body
    assert "show overload" not in body.lower()
    assert "enable" not in body.lower()


def test_overload_carries_no_encouragement(client, conn):
    """SPEC §9: "Do not add encouragement." Checked literally, because the
    temptation to soften this is exactly what the instruction anticipates."""
    crush_the_week(conn)
    add(conn, "Heavy", 20.0, 3)

    body = client.get("/").text.lower()

    for word in ("you've got this", "don't worry", "keep going", "great job",
                 "stay positive", "you can do it"):
        assert word not in body


def test_no_bare_score_appears_anywhere_on_today(client, conn):
    """SPEC §9 display rule 1: never show a bare score or rank number."""
    add(conn, "Something", 3.0, 2)

    body = client.get("/").text.lower()

    assert "priority:" not in body
    assert "score:" not in body
    assert "rank:" not in body


def test_overload_says_when_it_cannot_honestly_rank_the_sacrifices(client, conn):
    """Nothing has a points value or a late policy, so there is no honest answer."""
    crush_the_week(conn)
    conn.execute("UPDATE courses SET penalty_pct_per_day = NULL")
    add(conn, "Unknown cost", 20.0, 3, points_possible=None, course_id=None)

    body = client.get("/").text

    assert "no honest way" in body


# --- the timer --------------------------------------------------------------


def test_starting_the_clock_from_today(client, conn):
    item = add(conn, "Lab report", 3.0, 3)

    client.post(f"/assignments/{item}/timer/start")

    assert capacity.running(conn)["assignment_id"] == item


def test_the_running_clock_is_visible_on_every_page_load(client, conn):
    item = add(conn, "Lab report", 3.0, 3)
    client.post(f"/assignments/{item}/timer/start")

    body = client.get("/").text

    assert "Clock running on Lab report" in body
    assert "Stop" in body


def test_stopping_the_clock_books_the_time(client, conn):
    item = add(conn, "Lab report", 3.0, 3)
    client.post(f"/assignments/{item}/timer/start")

    client.post("/timer/stop", data={"note": ""})

    assert capacity.running(conn) is None
    assert conn.execute(
        "SELECT COUNT(*) n FROM time_entries WHERE ended_at IS NOT NULL"
    ).fetchone()["n"] == 1


def test_starting_the_clock_on_something_that_is_not_there_is_a_404(client):
    assert client.post("/assignments/999/timer/start").status_code == 404


# --- the capacity page ------------------------------------------------------


def test_the_capacity_page_shows_all_seven_days(client):
    body = client.get("/capacity").text
    for name in capacity.WEEKDAY_NAMES:
        assert name in body


def test_saving_the_week(client, conn):
    data = {f"productive_{day}": "3" for day in range(7)}
    data.update({f"practice_{day}": "1.5" for day in range(7)})
    data["productive_6"] = "8"

    client.post("/capacity", data=data)

    rows = capacity.settings(conn)
    assert rows[0]["productive_hours"] == 3.0
    assert rows[0]["practice_hours_target"] == 1.5
    assert rows[6]["productive_hours"] == 8.0


def test_nonsense_hours_are_refused_with_a_reason(client, conn):
    data = {f"productive_{day}": "4" for day in range(7)}
    data.update({f"practice_{day}": "0" for day in range(7)})
    data["productive_3"] = "quite a lot"

    body = client.post("/capacity", data=data, follow_redirects=True).text

    assert "must be numbers" in body
    assert capacity.settings(conn)[3]["productive_hours"] == 4.0


def test_an_impossible_day_is_refused(client, conn):
    data = {f"productive_{day}": "4" for day in range(7)}
    data.update({f"practice_{day}": "0" for day in range(7)})
    data["productive_1"] = "30"

    body = client.post("/capacity", data=data, follow_redirects=True).text

    assert "16 hours" in body
    assert capacity.settings(conn)[1]["productive_hours"] == 4.0


def test_adding_a_commitment(client, conn):
    client.post("/commitments", data={
        "label": "Wind Ensemble", "kind": "ensemble", "weekday": "2",
        "start_time": "16:00", "end_time": "18:00", "course_id": "",
    })

    rows = capacity.commitments(conn)
    assert len(rows) == 1
    assert rows[0]["label"] == "Wind Ensemble"
    assert rows[0]["weekday"] == 2


def test_a_backwards_commitment_is_refused(client, conn):
    body = client.post("/commitments", data={
        "label": "Backwards", "kind": "class", "weekday": "1",
        "start_time": "18:00", "end_time": "16:00", "course_id": "",
    }, follow_redirects=True).text

    assert "after the start time" in body
    assert capacity.commitments(conn) == []


def test_a_commitment_can_be_switched_off_without_losing_it(client, conn):
    """A rehearsal that ends after the concert may well come back next term."""
    client.post("/commitments", data={
        "label": "Wind Ensemble", "kind": "ensemble", "weekday": "2",
        "start_time": "16:00", "end_time": "18:00", "course_id": "",
    })
    item = capacity.commitments(conn)[0]["id"]

    client.post(f"/commitments/{item}/toggle")

    assert capacity.commitments(conn, active_only=True) == []
    assert len(capacity.commitments(conn, active_only=False)) == 1


def test_a_commitment_shows_up_in_the_week_it_shortens(client, conn):
    client.post("/commitments", data={
        "label": "All-day rehearsal", "kind": "ensemble", "weekday": "2",
        "start_time": "08:00", "end_time": "20:00", "course_id": "",
    })

    body = client.get("/capacity").text

    assert "All-day rehearsal" in body
    assert "committed" in body


def test_the_capacity_page_explains_why_practice_is_protected(client):
    """The reasoning is the feature. Without it this looks like a stray setting."""
    body = client.get("/capacity").text
    # Matched on unwrapped fragments — the sentence is wrapped in the template.
    assert "is protected" in body
    assert "lose every time" in body


# --- the weekly review ------------------------------------------------------


def test_the_review_puts_what_slipped_first(client, conn):
    add(conn, "Missed it", 2.0, -3)
    add(conn, "Coming up", 2.0, 3)

    body = client.get("/review").text

    assert body.index("Missed it") < body.index("Coming up")


def test_the_review_asks_for_missing_estimates(client, conn):
    add(conn, "Unestimated", None, 4, est_hours=None, est_hours_remaining=None)

    body = client.get("/review").text

    assert "Unestimated" in body
    assert "new estimate" in body


def test_the_review_states_the_hours_the_week_actually_holds(client, conn):
    body = client.get("/review").text
    assert "hours</strong> for coursework" in body


def test_the_review_is_calm_when_nothing_slipped(client, conn):
    add(conn, "Coming up", 2.0, 3)
    assert "Nothing overdue" in client.get("/review").text


def test_the_review_shows_what_was_finished_with_what_it_took(client, conn):
    item = add(conn, "Finished thing", 2.0, -1, status="submitted")
    conn.execute(
        "INSERT INTO time_entries (assignment_id, started_at, ended_at, minutes) "
        "VALUES (?, '2026-09-07T13:00:00Z', '2026-09-07T16:00:00Z', 180)",
        (item,),
    )

    body = client.get("/review").text

    assert "Finished thing" in body
    assert "took 3.0h" in body
