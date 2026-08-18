"""Tests for manual entry, syllabus batch entry, editing and courses (M2b).

These are the paths by which the six courses Canvas cannot see get into the
system at all (SPEC §6.3).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config, db, migrate
from app.main import app


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    # Mirror the server's timezone rather than the default UTC, so the dates these
    # tests assert on are the ones Mason would actually get.
    monkeypatch.setattr(config, "TZ", "America/Indiana/Indianapolis")
    migrate.run(path)
    return path


@pytest.fixture
def client(db_path):
    with TestClient(app) as test_client:
        yield test_client


def rows(path, sql, *args):
    conn = db.connect(path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def one(path, sql, *args):
    return rows(path, sql, *args)[0]


def text_of(response):
    return " ".join(response.text.split())


# --- adding one thing -------------------------------------------------------


def test_the_add_form_loads(client):
    body = text_of(client.get("/add"))
    assert "Add work" in body
    assert "How long will it take?" in body


def test_adding_an_assignment(client, db_path):
    response = client.post("/add", data={
        "title": "Species counterpoint 1", "type": "worksheet",
        "due": "2026-09-08", "hours": "2h",
    }, follow_redirects=False)
    assert response.status_code == 303

    row = one(db_path, "SELECT * FROM assignments")
    assert row["title"] == "Species counterpoint 1"
    assert row["type"] == "worksheet"
    assert row["est_hours"] == 2.0
    assert row["est_hours_remaining"] == 2.0
    assert row["source"] == "manual"
    assert row["due_at"].startswith("2026-09-09T03:59")  # 23:59 local


def test_an_added_assignment_appears_in_the_ranking(client, db_path):
    client.post("/add", data={"title": "Practice log", "type": "other",
                              "due": "2026-09-08", "hours": "1h"})
    assert "Practice log" in text_of(client.get("/"))


def test_a_paper_gets_a_start_date(client, db_path):
    """SPEC §5: due_at − (est_hours × 2 days), so long work does not lose."""
    client.post("/add", data={"title": "Term paper", "type": "paper",
                              "due": "2026-11-20", "hours": "6h"})
    row = one(db_path, "SELECT due_at, start_by FROM assignments")
    assert row["start_by"] is not None
    assert row["start_by"] < row["due_at"]


def test_a_worksheet_gets_no_start_date(client, db_path):
    client.post("/add", data={"title": "Worksheet", "type": "worksheet",
                              "due": "2026-11-20", "hours": "1h"})
    assert one(db_path, "SELECT start_by FROM assignments")["start_by"] is None


def test_an_unreadable_date_is_explained_not_swallowed(client, db_path):
    response = client.post("/add", data={"title": "Thing", "due": "whenever",
                                         "hours": "1h"}, follow_redirects=False)
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert rows(db_path, "SELECT * FROM assignments") == []

    # The typed title survives the round trip so it does not have to be retyped.
    assert "Thing" in text_of(client.get(response.headers["location"]))


def test_an_assignment_needs_a_title(client, db_path):
    client.post("/add", data={"title": "   ", "hours": "1h"})
    assert rows(db_path, "SELECT * FROM assignments") == []


def test_a_date_is_optional(client, db_path):
    client.post("/add", data={"title": "Someday thing", "hours": "1h"})
    assert one(db_path, "SELECT due_at FROM assignments")["due_at"] is None


# --- pasting a syllabus -----------------------------------------------------


SYLLABUS = (
    "Species counterpoint 1 | 2026-09-08 | 2h\n"
    "Species counterpoint 2 | 9/15 | 2h\n"
    "Listening journal wk3 | Sep 17\n"
    "Midterm exam | 10/6 | 6h\n"
)


def test_the_batch_form_loads(client):
    assert "Paste a syllabus" in text_of(client.get("/batch"))


def test_pasting_shows_a_preview_and_saves_nothing_yet(client, db_path):
    body = text_of(client.post("/batch", data={"pasted": SYLLABUS, "course_id": ""}))

    assert "What this would add" in body
    assert "Species counterpoint 1" in body
    assert "Midterm exam" in body
    assert "Save 4 assignments" in body
    # Nothing written until the second step.
    assert rows(db_path, "SELECT * FROM assignments") == []


def test_saving_the_batch_creates_them(client, db_path):
    response = client.post("/batch/save", data={"pasted": SYLLABUS, "course_id": ""},
                           follow_redirects=False)
    assert response.status_code == 303

    created = rows(db_path, "SELECT title, type, source FROM assignments ORDER BY id")
    assert len(created) == 4
    assert all(row["source"] == "syllabus_batch" for row in created)
    assert created[3]["type"] == "exam"


def test_a_batch_can_be_attached_to_a_course(client, db_path):
    client.post("/courses", data={"name": "Music Theory III", "code": "MUS-T 251"})
    course_id = one(db_path, "SELECT id FROM courses")["id"]

    client.post("/batch/save", data={"pasted": SYLLABUS, "course_id": str(course_id)})

    assert one(db_path, "SELECT COUNT(*) n FROM assignments WHERE course_id = ?",
               course_id)["n"] == 4


def test_a_bad_line_is_shown_and_the_rest_still_save(client, db_path):
    """Nothing disappears silently — the unreadable line is named."""
    pasted = "Good | 9/8 | 1h\nBad | someday | 1h\nAlso good | 9/9"

    preview = text_of(client.post("/batch", data={"pasted": pasted, "course_id": ""}))
    assert "1 line could not be read" in preview
    assert "Bad | someday | 1h" in preview
    assert "not a date" in preview
    assert "Save 2 assignments" in preview

    client.post("/batch/save", data={"pasted": pasted, "course_id": ""})
    titles = [r["title"] for r in rows(db_path, "SELECT title FROM assignments")]
    assert titles == ["Good", "Also good"]


def test_saving_an_empty_paste_does_nothing(client, db_path):
    client.post("/batch/save", data={"pasted": "   ", "course_id": ""})
    assert rows(db_path, "SELECT * FROM assignments") == []


def test_batch_items_without_hours_join_the_estimate_prompts(client, db_path):
    client.post("/batch/save", data={"pasted": "Journal | Sep 17", "course_id": ""})
    assert one(db_path, "SELECT est_hours FROM assignments")["est_hours"] is None
    assert "need an estimate" in text_of(client.get("/"))


# --- editing ----------------------------------------------------------------


def test_a_captured_note_can_be_turned_into_real_work(client, db_path):
    """The point of quick capture: dump it now, make it real later."""
    client.post("/capture", data={"text": "counterpoint exercise Dr Reyes mentioned"})
    assignment_id = one(db_path, "SELECT id FROM assignments")["id"]

    assert "Add details" in text_of(client.get("/"))

    client.post(f"/assignments/{assignment_id}/edit", data={
        "title": "Counterpoint exercise", "type": "worksheet",
        "due": "2026-09-08", "hours": "2h",
    })

    row = one(db_path, "SELECT * FROM assignments")
    assert row["title"] == "Counterpoint exercise"
    assert row["est_hours"] == 2.0
    assert row["due_at"] is not None


def test_the_edit_form_is_filled_in_already(client, db_path):
    client.post("/add", data={"title": "Essay", "type": "paper",
                              "due": "2026-09-08", "hours": "6h"})
    assignment_id = one(db_path, "SELECT id FROM assignments")["id"]

    body = client.get(f"/assignments/{assignment_id}/edit").text
    assert 'value="Essay"' in body
    assert "2026-09-08" in body
    assert 'value="6"' in body


def test_changing_a_due_date_supersedes_pending_reminders(client, db_path):
    """SPEC §5, the same rule the Canvas feed follows."""
    client.post("/add", data={"title": "Essay", "due": "2026-09-08", "hours": "2h"})
    assignment_id = one(db_path, "SELECT id FROM assignments")["id"]

    conn = db.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO reminder_instances (assignment_id, kind, fire_at, channel, state) "
            "VALUES (?, 'due_by', '2026-09-07T12:00:00Z', 'caldav', 'pending')",
            (assignment_id,),
        )
    finally:
        conn.close()

    client.post(f"/assignments/{assignment_id}/edit", data={
        "title": "Essay", "type": "other", "due": "2026-09-15", "hours": "2h"})

    assert one(db_path, "SELECT state FROM reminder_instances")["state"] == "superseded"


def test_editing_something_that_does_not_exist_is_a_404(client):
    assert client.get("/assignments/9999/edit").status_code == 404


def test_a_due_date_can_be_removed(client, db_path):
    client.post("/add", data={"title": "Essay", "due": "2026-09-08", "hours": "2h"})
    assignment_id = one(db_path, "SELECT id FROM assignments")["id"]
    client.post(f"/assignments/{assignment_id}/edit", data={
        "title": "Essay", "type": "other", "due": "", "hours": "2h"})
    assert one(db_path, "SELECT due_at FROM assignments")["due_at"] is None


# --- courses ----------------------------------------------------------------


def test_the_courses_page_loads(client):
    assert "Add a course" in text_of(client.get("/courses"))


def test_adding_a_course_canvas_cannot_see(client, db_path):
    client.post("/courses", data={
        "name": "Music Theory III", "code": "MUS-T 251",
        "instructor": "Dr Reyes", "meeting_pattern": "MWF 10:10-11:00",
        "credits": "3", "late_policy": "10% per day",
    })

    row = one(db_path, "SELECT * FROM courses")
    assert row["name"] == "Music Theory III"
    assert row["instructor"] == "Dr Reyes"
    assert row["credits"] == 3.0
    assert row["late_policy"] == "10% per day"
    # Named by hand, so it is not asking to be renamed.
    assert row["needs_naming"] == 0


def test_a_course_only_needs_a_name(client, db_path):
    client.post("/courses", data={"name": "Applied Lessons"})
    assert one(db_path, "SELECT name FROM courses")["name"] == "Applied Lessons"


def test_a_course_with_no_name_is_not_created(client, db_path):
    client.post("/courses", data={"name": "  "})
    assert rows(db_path, "SELECT * FROM courses") == []


def test_a_term_is_created_when_there_is_none(client, db_path):
    """courses.term_id is NOT NULL, and inventing a term should not be Mason's job."""
    client.post("/courses", data={"name": "Applied Lessons"})
    terms = rows(db_path, "SELECT name, needs_dates FROM terms")
    assert len(terms) == 1
    assert terms[0]["needs_dates"] == 1


def test_editing_a_course_records_what_m6_will_need(client, db_path):
    client.post("/courses", data={"name": "Biology"})
    course_id = one(db_path, "SELECT id FROM courses")["id"]

    client.post(f"/courses/{course_id}/edit", data={
        "name": "Biology L112", "code": "BIOL-L112", "instructor": "Dr Vance",
        "meeting_pattern": "TR 09:30-10:45", "credits": "4",
        "late_policy": "no late work", "current_grade_pct": "88",
    })

    row = one(db_path, "SELECT * FROM courses")
    assert row["name"] == "Biology L112"
    assert row["current_grade_pct"] == 88.0
    assert row["late_policy"] == "no late work"


def test_an_impossible_grade_is_ignored_rather_than_stored(client, db_path):
    client.post("/courses", data={"name": "Biology"})
    course_id = one(db_path, "SELECT id FROM courses")["id"]
    client.post(f"/courses/{course_id}/edit",
                data={"name": "Biology", "current_grade_pct": "250"})
    assert one(db_path, "SELECT current_grade_pct v FROM courses")["v"] is None


def test_editing_a_course_clears_the_needs_naming_flag(client, db_path):
    conn = db.connect(db_path)
    try:
        conn.execute("INSERT INTO terms (id,name,start_date,end_date) "
                     "VALUES (1,'FA26','2026-08-01','2026-12-15')")
        conn.execute("INSERT INTO courses (id,term_id,name,needs_naming) "
                     "VALUES (1,1,'FA26-BL-MATH-M211-2050',1)")
    finally:
        conn.close()

    client.post("/courses/1/edit", data={"name": "Calculus I", "code": "MATH-M211"})
    assert one(db_path, "SELECT needs_naming n FROM courses")["n"] == 0


def test_courses_offer_the_assignment_count(client, db_path):
    client.post("/courses", data={"name": "Biology"})
    course_id = one(db_path, "SELECT id FROM courses")["id"]
    client.post("/add", data={"title": "Lab 1", "course_id": str(course_id),
                              "due": "2026-09-08", "hours": "2h"})
    assert "1 assignment" in text_of(client.get("/courses"))


# --- navigation -------------------------------------------------------------


def test_today_links_to_all_the_entry_paths(client):
    body = client.get("/").text
    for path in ("/add", "/batch", "/courses"):
        assert f'href="{path}"' in body


def test_the_preview_shows_the_date_that_was_typed(client, db_path):
    """A deadline of 23:59 local is 03:59 the next day in UTC.

    Echoing the stored timestamp back would show 9 September for a line reading
    8 September, which reads as the parser having got it wrong.
    """
    body = text_of(client.post("/batch", data={
        "pasted": "Counterpoint 1 | 2026-09-08 | 2h", "course_id": ""}))
    assert "Tue 8 Sep 2026" in body
    assert "9 Sep 2026" not in body
