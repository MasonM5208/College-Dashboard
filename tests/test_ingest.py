"""Tests for the ingest endpoint and the archive pages.

The endpoint is what the iPhone Shortcut talks to, so the things that matter are
the ones nobody can debug from a phone: a wrong token has to fail clearly, a
duplicate has to succeed, and no response may ever contain the token.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import archive, config, db, migrate
from app.main import app

TOKEN = "test-token-not-a-real-secret"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "TZ", "America/Indiana/Indianapolis")
    monkeypatch.setenv("INGEST_TOKEN", TOKEN)
    migrate.run(path)
    connection = db.connect(path)
    try:
        connection.execute(
            "INSERT INTO terms (id,name,start_date,end_date) "
            "VALUES (1,'FA26','2026-08-24','2026-12-18')"
        )
        connection.execute(
            "INSERT INTO courses (id,term_id,name,code) VALUES (1,1,'Biology 105','BIOL 105')"
        )
        yield connection
    finally:
        connection.close()


@pytest.fixture
def client(conn):
    with TestClient(app) as test_client:
        yield test_client


def auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


# --- the endpoint -----------------------------------------------------------


def test_a_shortcut_can_save_a_message(client, conn):
    response = client.post(
        "/ingest", json={"body": "Lab 3 has moved to Friday."}, headers=auth()
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["created"] is True
    assert payload["url"] == f"/archive/{payload['document_id']}"
    assert archive.count(conn) == 1


def test_the_share_sheet_is_the_assumed_source(client, conn):
    response = client.post("/ingest", json={"body": "something"}, headers=auth())
    sources = archive.sources_for(conn, response.json()["document_id"])
    assert [row["source"] for row in sources] == ["share_sheet"]


def test_form_encoding_works_too(client, conn):
    """Which of the two is easier to build in Shortcuts varies by iOS version."""
    response = client.post("/ingest", data={"body": "posted as a form"}, headers=auth())
    assert response.status_code == 200
    assert archive.count(conn) == 1


def test_sharing_the_same_thing_twice_is_a_success_not_an_error(client, conn):
    body = "Lab 3 has moved to Friday."
    first = client.post("/ingest", json={"body": body}, headers=auth())
    second = client.post("/ingest", json={"body": body + "\n\nSent from my iPhone"},
                         headers=auth())

    assert second.status_code == 200
    assert second.json()["created"] is False
    assert second.json()["document_id"] == first.json()["document_id"]
    assert archive.count(conn) == 1


def test_no_token_is_refused(client, conn):
    assert client.post("/ingest", json={"body": "x"}).status_code == 401
    assert archive.count(conn) == 0


def test_the_wrong_token_is_refused(client, conn):
    response = client.post("/ingest", json={"body": "x"}, headers=auth("wrong"))
    assert response.status_code == 401
    assert archive.count(conn) == 0


def test_a_refusal_never_echoes_either_token(client):
    response = client.post("/ingest", json={"body": "x"}, headers=auth("wrong"))
    assert TOKEN not in response.text
    assert "wrong" not in response.text


def test_an_unset_token_says_so_without_accepting_anything(client, conn, monkeypatch):
    monkeypatch.delenv("INGEST_TOKEN", raising=False)

    response = client.post("/ingest", json={"body": "x"}, headers=auth())

    assert response.status_code == 503
    assert "INGEST_TOKEN" in response.text          # names the setting
    assert archive.count(conn) == 0


def test_a_request_with_no_body_field_is_a_clear_400(client):
    response = client.post("/ingest", json={"subject": "only a subject"}, headers=auth())
    assert response.status_code == 400
    assert "body" in response.text


def test_broken_json_is_a_400_rather_than_a_500(client):
    response = client.post(
        "/ingest",
        content=b"{not json",
        headers={**auth(), "Content-Type": "application/json"},
    )
    assert response.status_code == 400


def test_an_oversized_body_is_refused(client, conn):
    response = client.post(
        "/ingest", json={"body": "x" * (archive.MAX_BODY_BYTES + 1)}, headers=auth()
    )
    assert response.status_code == 400
    assert archive.count(conn) == 0


def test_the_optional_fields_are_recorded(client, conn):
    response = client.post(
        "/ingest",
        json={
            "body": "Lab 3 has moved.",
            "subject": "Lab 3",
            "sender": "aruiz@iu.edu",
            "received_at": "2026-09-01T13:14:00Z",
            "kind": "email",
            "external_id": "<abc@mail.iu.edu>",
        },
        headers=auth(),
    )

    document = archive.get(conn, response.json()["document_id"])
    assert document["subject"] == "Lab 3"
    assert document["sender"] == "aruiz@iu.edu"
    assert document["kind"] == "email"
    assert archive.sources_for(conn, document["id"])[0]["external_id"] == "<abc@mail.iu.edu>"


def test_an_unknown_kind_becomes_other_rather_than_failing(client, conn):
    """A Shortcut with a typo in it should still capture the message."""
    response = client.post(
        "/ingest", json={"body": "x", "kind": "postcard"}, headers=auth()
    )
    assert response.status_code == 200
    assert archive.get(conn, response.json()["document_id"])["kind"] == "other"


# --- the pages --------------------------------------------------------------


def test_the_archive_page_loads_when_empty(client):
    body = client.get("/archive").text
    assert "Nothing saved yet" in body


def test_pasting_something_in(client, conn):
    response = client.post(
        "/archive/add",
        data={"body": "The recital is on Thursday.", "subject": "Recital",
              "kind": "email", "course_id": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    document_id = int(response.headers["location"].split("/")[2].split("?")[0])
    assert archive.get(conn, document_id)["subject"] == "Recital"
    assert [row["id"] for row in archive.courses_for(conn, document_id)] == [1]


def test_pasting_a_duplicate_says_so_rather_than_claiming_a_new_save(client, conn):
    archive.ingest(conn, "The recital is on Thursday.", source="share_sheet")

    response = client.post(
        "/archive/add", data={"body": "The recital is on Thursday."},
        follow_redirects=True,
    )

    assert "Already had this one" in response.text
    assert archive.count(conn) == 1


def test_pasting_nothing_says_what_is_wrong(client, conn):
    response = client.post("/archive/add", data={"body": "   "}, follow_redirects=True)
    assert "nothing to save" in response.text.lower()
    assert archive.count(conn) == 0


def test_searching_from_the_page(client, conn):
    archive.ingest(conn, "The lab report is due Friday.", source="paste",
                   subject="Lab report")
    archive.ingest(conn, "Nothing to do with it.", source="paste", subject="Other")

    body = client.get("/archive?q=report").text

    assert "Lab report" in body
    assert "Other" not in body
    assert "<mark>" in body


def test_a_search_with_no_matches_says_how_big_the_archive_is(client, conn):
    archive.ingest(conn, "something entirely different", source="paste")
    body = client.get("/archive?q=nonexistentword").text
    assert "Nothing matches" in body


def test_the_document_page_shows_the_body_untouched(client, conn):
    body = "Line one.\n\n  **not bold**\n\nLine two."
    result = archive.ingest(conn, body, source="paste", subject="Verbatim")

    page = client.get(f"/archive/{result.document_id}").text

    # Rendered as text, not as Markdown: the archive shows what arrived.
    assert "**not bold**" in page
    assert "<strong>not bold</strong>" not in page


def test_the_document_page_escapes_what_it_shows(client, conn):
    result = archive.ingest(conn, "<script>alert(1)</script>", source="paste")
    page = client.get(f"/archive/{result.document_id}").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_the_document_page_names_every_route_it_arrived_by(client, conn):
    result = archive.ingest(conn, "Lab 3 has moved to Friday.", source="share_sheet")
    archive.ingest(conn, "Lab 3 has moved to Friday.\n\n--\nAna", source="paste")

    page = client.get(f"/archive/{result.document_id}").text

    assert "shared from your phone" in page
    assert "pasted in" in page
    assert "2 different routes" in page


def test_a_document_that_is_not_there_is_a_404(client):
    assert client.get("/archive/999").status_code == 404


def test_attaching_and_detaching_a_course_from_the_page(client, conn):
    result = archive.ingest(conn, "The lab report is due Friday.", source="paste")

    client.post(f"/archive/{result.document_id}/link", data={"course_id": "1"})
    assert [row["id"] for row in archive.courses_for(conn, result.document_id)] == [1]

    client.post(f"/archive/{result.document_id}/unlink", data={"course_id": "1"})
    assert archive.courses_for(conn, result.document_id) == []


def test_deleting_asks_first(client, conn):
    result = archive.ingest(conn, "A mis-paste.", source="paste")

    page = client.get(f"/archive/{result.document_id}?deleting={result.document_id}").text

    assert "Yes, delete it" in page
    assert archive.count(conn) == 1


def test_deleting_a_document(client, conn):
    result = archive.ingest(conn, "A mis-paste.", source="paste")

    response = client.post(f"/archive/{result.document_id}/delete", follow_redirects=False)

    assert response.headers["location"] == "/archive"
    assert archive.count(conn) == 0


def test_filtering_the_page_by_course(client, conn):
    bio = archive.ingest(conn, "The lab report is due Friday.", source="paste",
                         subject="Bio lab")
    archive.ingest(conn, "The lab section for calculus.", source="paste",
                   subject="Calculus lab")
    archive.link_course(conn, bio.document_id, 1)

    body = client.get("/archive?q=lab&course=1").text

    assert "Bio lab" in body
    assert "Calculus lab" not in body


def test_the_reply_carries_a_sentence_for_the_phone_to_show(client, conn):
    """The Shortcut shows this in a notification; raw JSON would not do."""
    body = "The recital is on Thursday."
    first = client.post("/ingest", json={"body": body}, headers=auth())
    second = client.post("/ingest", json={"body": body}, headers=auth())

    assert first.json()["message"] == "Saved to the archive."
    assert "Already had this one" in second.json()["message"]
