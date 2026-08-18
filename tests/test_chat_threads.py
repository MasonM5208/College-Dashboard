"""Tests for conversations: starting, naming, keeping and deleting them.

The behaviour under test is separation. Before this, every question was appended
to the most recently used thread, so a question about a calculus limit and a
question about a paper deadline shared one transcript — unreadable to scroll, and
paid for twice over, because the whole thread is re-sent to the model on every
turn.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config, db, migrate
from app.main import app


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "TZ", "America/Indiana/Indianapolis")
    migrate.run(path)
    connection = db.connect(path)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def client(conn):
    with TestClient(app) as test_client:
        yield test_client


def ask(client, question, thread=""):
    """Post a question and return the thread it landed in."""
    response = client.post(
        "/chat/send",
        data={"question": question, "thread": thread},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].rsplit("=", 1)[1]


def make_thread(conn, thread_id, title, *, pinned=0, updated="2026-08-01T12:00:00Z"):
    conn.execute(
        "INSERT INTO chat_threads (id, title, pinned, updated_at) VALUES (?,?,?,?)",
        (thread_id, title, pinned, updated),
    )
    conn.execute(
        "INSERT INTO chat_messages (thread_id, role, content) VALUES (?, 'user', ?)",
        (thread_id, f"opening question for {title}"),
    )
    # The trigger on chat_messages bumps updated_at to now, which would make every
    # fixture thread look equally recent and destroy the ordering under test.
    conn.execute(
        "UPDATE chat_threads SET updated_at = ? WHERE id = ?", (updated, thread_id)
    )


# --- separation -------------------------------------------------------------


def test_a_new_question_does_not_join_the_last_conversation(client, conn):
    """The bug this migration exists to fix."""
    first = ask(client, "when is my bio lab due?")
    second = ask(client, "explain the chain rule")

    assert first != second
    assert conn.execute("SELECT COUNT(*) n FROM chat_threads").fetchone()["n"] == 2


def test_asking_inside_a_conversation_stays_in_it(client, conn):
    first = ask(client, "when is my bio lab due?")
    again = ask(client, "and the one after that?", thread=first)

    assert again == first
    count = conn.execute(
        "SELECT COUNT(*) n FROM chat_messages WHERE thread_id = ?", (first,)
    ).fetchone()["n"]
    assert count == 2


def test_the_chat_page_opens_a_new_conversation_by_default(client, conn):
    make_thread(conn, 1, "an older conversation")

    body = client.get("/chat").text

    # The old conversation is listed, but its messages are not on the page: this
    # is a blank one, and the hidden field that carries the thread is empty.
    assert "an older conversation" in body
    assert "opening question for" not in body
    assert 'name="thread" value=""' in body


def test_opening_a_conversation_shows_its_messages(client, conn):
    make_thread(conn, 1, "an older conversation")

    body = client.get("/chat?thread=1").text

    assert "opening question for an older conversation" in body
    assert 'name="thread" value="1"' in body


def test_a_deleted_conversation_still_open_elsewhere_starts_a_fresh_one(client, conn):
    """A stale tab or bookmark must not 404, and must not resurrect the id."""
    body = client.get("/chat?thread=999")
    assert body.status_code == 200
    assert 'name="thread" value=""' in body.text

    ask(client, "a question from the stale tab", thread="999")
    rows = conn.execute("SELECT id FROM chat_threads").fetchall()
    assert [row["id"] for row in rows] != [999]
    assert len(rows) == 1


# --- naming -----------------------------------------------------------------


def test_a_conversation_is_named_after_its_opening_question(client, conn):
    ask(client, "how much reading is left this week?")
    title = conn.execute("SELECT title FROM chat_threads").fetchone()["title"]
    assert title == "how much reading is left this week?"


def test_a_multiline_question_does_not_become_a_multiline_title(client, conn):
    ask(client, "first line\n\n  second line  ")
    title = conn.execute("SELECT title FROM chat_threads").fetchone()["title"]
    assert title == "first line second line"


def test_renaming_a_conversation(client, conn):
    make_thread(conn, 1, "what")

    client.post("/chat/1/rename", data={"title": "Bio 105 lab writeups"})

    assert conn.execute(
        "SELECT title FROM chat_threads WHERE id = 1"
    ).fetchone()["title"] == "Bio 105 lab writeups"


def test_an_emptied_name_falls_back_to_the_placeholder(client, conn):
    make_thread(conn, 1, "what")

    client.post("/chat/1/rename", data={"title": "   "})

    assert conn.execute(
        "SELECT title FROM chat_threads WHERE id = 1"
    ).fetchone()["title"] is None
    assert "Untitled conversation" in client.get("/chat/threads").text


def test_renaming_something_that_is_not_there_is_a_404(client):
    assert client.post("/chat/999/rename", data={"title": "x"}).status_code == 404


# --- keeping ----------------------------------------------------------------


def test_kept_conversations_sort_above_more_recent_ones(client, conn):
    make_thread(conn, 1, "the kept one", updated="2026-01-01T00:00:00Z")
    make_thread(conn, 2, "a more recent one", updated="2026-08-17T00:00:00Z")

    client.post("/chat/1/keep")

    body = client.get("/chat/threads").text
    assert body.index("the kept one") < body.index("a more recent one")


def test_keeping_is_a_toggle(client, conn):
    make_thread(conn, 1, "one")

    client.post("/chat/1/keep")
    assert conn.execute("SELECT pinned FROM chat_threads").fetchone()["pinned"] == 1

    client.post("/chat/1/keep")
    assert conn.execute("SELECT pinned FROM chat_threads").fetchone()["pinned"] == 0


# --- deleting ---------------------------------------------------------------


def test_deleting_asks_first(client, conn):
    make_thread(conn, 1, "one to lose")

    body = client.get("/chat/threads?deleting=1").text

    # The wording is line-wrapped in the template, so match the button.
    assert "Yes, delete it" in body
    assert "Delete this conversation and its" in body
    assert conn.execute("SELECT COUNT(*) n FROM chat_threads").fetchone()["n"] == 1


def test_deleting_takes_the_messages_with_it(client, conn):
    make_thread(conn, 1, "one to lose")
    make_thread(conn, 2, "one to keep")

    client.post("/chat/1/delete", follow_redirects=False)

    assert conn.execute(
        "SELECT COUNT(*) n FROM chat_messages WHERE thread_id = 1"
    ).fetchone()["n"] == 0
    assert [row["id"] for row in conn.execute("SELECT id FROM chat_threads")] == [2]


def test_deleting_does_not_land_back_on_the_deleted_conversation(client, conn):
    make_thread(conn, 1, "one to lose")

    response = client.post(
        "/chat/1/delete",
        headers={"referer": "http://testserver/chat?thread=1&deleting=1"},
        follow_redirects=False,
    )

    assert response.headers["location"] == "/chat"


def test_deleting_from_the_list_returns_to_the_list(client, conn):
    make_thread(conn, 1, "one to lose")

    response = client.post(
        "/chat/1/delete",
        headers={"referer": "http://testserver/chat/threads?deleting=1"},
        follow_redirects=False,
    )

    assert response.headers["location"] == "/chat/threads"


def test_deleting_something_that_is_not_there_is_a_404(client):
    assert client.post("/chat/999/delete").status_code == 404


# --- the list ---------------------------------------------------------------


def test_the_list_previews_the_last_answer(client, conn):
    make_thread(conn, 1, "a question")
    conn.execute(
        "INSERT INTO chat_messages (thread_id, role, content) "
        "VALUES (1, 'assistant', 'The lab report is due Friday at 11:59pm.')"
    )

    body = client.get("/chat/threads").text

    assert "The lab report is due Friday at 11:59pm." in body


def test_a_long_preview_is_cut_rather_than_wrapped(client, conn):
    make_thread(conn, 1, "a question")
    conn.execute(
        "INSERT INTO chat_messages (thread_id, role, content) VALUES (1,'assistant',?)",
        ("word " * 200,),
    )

    body = client.get("/chat/threads").text

    assert "…" in body
    # The whole 1000-character answer must not be sitting in the list markup.
    assert "word " * 100 not in body


def test_the_chat_page_stops_listing_after_a_handful(client, conn):
    for index in range(1, 16):
        make_thread(conn, index, f"conversation {index}",
                    updated=f"2026-08-{index:02d}T00:00:00Z")

    body = client.get("/chat").text

    # Newest first, so the recent ones are there and the oldest are behind a link.
    assert "conversation 15" in body
    assert "conversation 1 " not in body
    assert "more" in body
    assert "/chat/threads" in body


def test_the_list_page_shows_them_all(client, conn):
    for index in range(1, 16):
        make_thread(conn, index, f"conversation {index}",
                    updated=f"2026-08-{index:02d}T00:00:00Z")

    body = client.get("/chat/threads").text

    for index in range(1, 16):
        assert f"conversation {index}" in body


def test_the_list_page_is_calm_when_there_is_nothing_yet(client):
    assert "No conversations yet" in client.get("/chat/threads").text
