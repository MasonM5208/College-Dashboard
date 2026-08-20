"""Tests for the document archive (SPEC §7): normalising, dedup and search.

The most load-bearing function here is `normalize`. It decides whether the same
message arriving by two routes becomes one document or two, and SPEC §7 is blunt
about the cost of getting that wrong later: "retrofitting dedup across 800
documents is miserable". So the cases below are shaped like real mail — Gmail's
"On ... wrote:", Outlook's header block, a phone's "Sent from my iPhone" — rather
than like tidy fixtures.
"""

from __future__ import annotations

import sqlite3

import pytest

from app import archive, config, db, migrate


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", path)
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
        connection.execute(
            "INSERT INTO courses (id,term_id,name,code) VALUES (2,1,'Calculus II','MATH 211')"
        )
        yield connection
    finally:
        connection.close()


# The same message, as it actually looks arriving three different ways.

REPLY = """Hi Mason,

Lab 3 has moved to Friday. Bring the notebook.

Ana
"""

FROM_GMAIL = REPLY + """
On Mon, 1 Sep 2026 at 09:14, Mason Miller <mm@iu.edu> wrote:
> When is lab 3?
> Thanks
"""

FROM_PHONE = """Hi Mason,
Lab 3 has moved to Friday.  Bring the notebook.
Ana

Sent from my iPhone
"""

FROM_OUTLOOK = REPLY + """
-----Original Message-----
From: Mason Miller <mm@iu.edu>
Sent: Monday, September 1, 2026 9:14 AM
To: Ana Ruiz
Subject: lab

When is lab 3?
"""


# --- normalising ------------------------------------------------------------


def test_the_same_message_by_three_routes_has_one_fingerprint():
    """The single most important assertion in this file."""
    assert (
        archive.body_hash(FROM_GMAIL)
        == archive.body_hash(FROM_PHONE)
        == archive.body_hash(FROM_OUTLOOK)
    )


def test_two_different_messages_do_not_collide():
    other = REPLY.replace("Friday", "Monday")
    assert archive.body_hash(REPLY) != archive.body_hash(other)


def test_a_gmail_quote_chain_is_stripped():
    assert "when is lab 3" not in archive.normalize(FROM_GMAIL)
    assert "notebook" in archive.normalize(FROM_GMAIL)


def test_angle_bracket_quoting_is_stripped():
    body = "My answer.\n\n> the original question\n> second line\n"
    assert archive.normalize(body) == "my answer."


def test_an_outlook_header_block_is_stripped():
    body = "My answer.\n\nFrom: Someone <s@iu.edu>\nSent: Monday\nTo: Mason\n\nOriginal.\n"
    assert archive.normalize(body) == "my answer."


def test_a_message_that_merely_opens_by_naming_a_sender_is_not_cut():
    """"From:" alone is not a quote marker — plenty of real messages start that way."""
    body = "From: the department newsletter\n\nThe recital is Thursday.\n"
    assert "recital is thursday" in archive.normalize(body)


def test_an_rfc_signature_delimiter_ends_the_message():
    body = "The recital is Thursday.\n\n--\nAna Ruiz\nDepartment of Biology\n555-0100\n"
    assert archive.normalize(body) == "the recital is thursday."


def test_a_phone_footer_is_dropped_from_the_end_only():
    """Quoted mid-message, it is part of what was said."""
    trailing = "Bring the notebook.\n\nSent from my iPhone\n"
    quoted = "He wrote 'Sent from my iPhone' at the bottom.\n\nBring the notebook.\n"

    assert archive.normalize(trailing) == "bring the notebook."
    assert "sent from my iphone" in archive.normalize(quoted)


def test_curly_and_straight_quotes_are_the_same_message():
    curly = "Don’t forget the lab — it’s Friday."
    straight = "Don't forget the lab - it's Friday."
    assert archive.body_hash(curly) == archive.body_hash(straight)


def test_line_wrapping_does_not_change_the_fingerprint():
    wrapped = "The lab report is due on\nFriday at noon."
    flowed = "The lab report is due on Friday at noon."
    assert archive.body_hash(wrapped) == archive.body_hash(flowed)


def test_a_subject_is_taken_from_the_first_real_line():
    assert archive.derive_subject("\n\n  Lab 3 moved  \nDetails below\n") == "Lab 3 moved"
    assert archive.derive_subject("   \n\n") is None


# --- saving and dedup -------------------------------------------------------


def test_saving_keeps_the_body_exactly_as_it_arrived(conn):
    result = archive.ingest(conn, FROM_GMAIL, source="paste")
    stored = archive.get(conn, result.document_id)["body"]

    # Not normalised, not stripped, not re-wrapped. Byte for byte.
    assert stored == FROM_GMAIL


def test_the_same_message_from_two_routes_is_one_document_with_two_sources(conn):
    first = archive.ingest(conn, FROM_GMAIL, source="share_sheet")
    second = archive.ingest(conn, FROM_OUTLOOK, source="paste")

    assert first.created is True
    assert second.created is False
    assert second.document_id == first.document_id
    assert second.source_added is True

    assert archive.count(conn) == 1
    sources = archive.sources_for(conn, first.document_id)
    assert sorted(row["source"] for row in sources) == ["paste", "share_sheet"]


def test_the_same_message_from_the_same_route_twice_adds_nothing(conn):
    first = archive.ingest(conn, FROM_GMAIL, source="paste")
    second = archive.ingest(conn, FROM_GMAIL, source="paste")

    assert second.document_id == first.document_id
    assert second.source_added is False
    assert len(archive.sources_for(conn, first.document_id)) == 1


def test_the_first_copy_is_the_one_kept(conn):
    """A duplicate never overwrites the body already held."""
    first = archive.ingest(conn, FROM_GMAIL, source="share_sheet")
    archive.ingest(conn, FROM_PHONE, source="paste")

    assert archive.get(conn, first.document_id)["body"] == FROM_GMAIL


def test_a_missing_subject_comes_from_the_message(conn):
    result = archive.ingest(conn, "Lab 3 moved\n\nDetails below.", source="share_sheet")
    assert archive.get(conn, result.document_id)["subject"] == "Lab 3 moved"


def test_an_empty_body_is_refused(conn):
    with pytest.raises(archive.ArchiveError):
        archive.ingest(conn, "   \n\n", source="paste")


def test_a_body_that_is_nothing_but_quoting_is_refused(conn):
    """Otherwise every such arrival normalises to "" and shares one hash."""
    with pytest.raises(archive.ArchiveError, match="quoted reply"):
        archive.ingest(conn, "> the original\n> and more of it\n", source="paste")


def test_an_enormous_body_is_refused(conn):
    with pytest.raises(archive.ArchiveError, match="KB"):
        archive.ingest(conn, "x" * (archive.MAX_BODY_BYTES + 1), source="paste")


def test_an_unknown_source_is_refused(conn):
    with pytest.raises(archive.ArchiveError):
        archive.ingest(conn, "hello", source="carrier_pigeon")


def test_every_save_is_audited(conn):
    archive.ingest(conn, FROM_GMAIL, source="share_sheet")
    archive.ingest(conn, FROM_PHONE, source="paste")

    rows = conn.execute(
        "SELECT action FROM audit_log WHERE table_name = 'documents' ORDER BY id"
    ).fetchall()
    assert [row["action"] for row in rows] == ["create", "source_added"]


# --- immutability -----------------------------------------------------------


def test_the_database_itself_refuses_to_rewrite_a_body(conn):
    """SPEC §5 asks for application-level enforcement; this is the backstop."""
    result = archive.ingest(conn, FROM_GMAIL, source="paste")

    with pytest.raises(sqlite3.IntegrityError, match="verbatim"):
        conn.execute(
            "UPDATE documents SET body = 'rewritten' WHERE id = ?", (result.document_id,)
        )

    assert archive.get(conn, result.document_id)["body"] == FROM_GMAIL


def test_a_subject_can_still_be_corrected(conn):
    """Only the record of what was said is frozen, not the filing around it."""
    result = archive.ingest(conn, FROM_GMAIL, source="paste", subject="lba 3")

    conn.execute(
        "UPDATE documents SET subject = 'Lab 3 moved' WHERE id = ?", (result.document_id,)
    )

    assert archive.get(conn, result.document_id)["subject"] == "Lab 3 moved"
    # And the correction reached the search index.
    assert [row["id"] for row in archive.search(conn, "Lab 3 moved")] == [result.document_id]


# --- searching --------------------------------------------------------------


def test_a_saved_message_is_findable_by_a_word_in_it(conn):
    result = archive.ingest(conn, FROM_GMAIL, source="paste")
    assert [row["id"] for row in archive.search(conn, "notebook")] == [result.document_id]


def test_words_match_from_the_start(conn):
    archive.ingest(conn, "The laboratory sections have moved.", source="paste")
    assert len(archive.search(conn, "lab")) == 1


def test_the_subject_outranks_the_body(conn):
    archive.ingest(conn, "A message that mentions counterpoint in passing.",
                   source="paste", subject="Room change")
    wanted = archive.ingest(conn, "Details of the assignment are attached.",
                            source="paste", subject="Counterpoint exercise 1")

    results = archive.search(conn, "counterpoint")
    assert results[0]["id"] == wanted.document_id


def test_search_terms_that_are_fts5_operators_do_not_crash(conn):
    archive.ingest(conn, "The midterm is near the end of October.", source="paste")

    for hostile in ['"', 'NEAR', 'AND', '*', 'OR NOT', '"unbalanced', 'a b" OR "c']:
        archive.search(conn, hostile)  # must not raise

    assert len(archive.search(conn, "NEAR")) == 1


def test_an_empty_search_returns_nothing_rather_than_everything(conn):
    archive.ingest(conn, "something", source="paste")
    assert archive.search(conn, "   ") == []


def test_a_snippet_cannot_inject_markup(conn):
    archive.ingest(conn, "Careful: <script>alert(1)</script> is in this midterm note.",
                   source="paste")

    rendered = archive.snippet_html(archive.search(conn, "midterm")[0]["body_snippet"])

    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "<mark>midterm</mark>" in rendered


def test_search_can_be_narrowed_to_one_course(conn):
    bio = archive.ingest(conn, "The lab report is due Friday.", source="paste")
    archive.ingest(conn, "The lab section for calculus does not exist.", source="paste")
    archive.link_course(conn, bio.document_id, 1)

    assert len(archive.search(conn, "lab")) == 2
    narrowed = archive.search(conn, "lab", course_id=1)
    assert [row["id"] for row in narrowed] == [bio.document_id]


def test_recent_shows_the_newest_first_including_undated_saves(conn):
    archive.ingest(conn, "An old one.", source="paste", received_at="2026-01-01T00:00:00Z")
    just_saved = archive.ingest(conn, "No date on this one.", source="share_sheet")

    assert archive.recent(conn)[0]["id"] == just_saved.document_id


# --- links ------------------------------------------------------------------


def test_attaching_and_detaching_a_course(conn):
    result = archive.ingest(conn, FROM_GMAIL, source="paste")

    archive.link_course(conn, result.document_id, 1)
    assert [row["id"] for row in archive.courses_for(conn, result.document_id)] == [1]

    archive.unlink_course(conn, result.document_id, 1)
    assert archive.courses_for(conn, result.document_id) == []


def test_attaching_the_same_course_twice_is_harmless(conn):
    result = archive.ingest(conn, FROM_GMAIL, source="paste")
    archive.link_course(conn, result.document_id, 1)
    archive.link_course(conn, result.document_id, 1)
    assert len(archive.courses_for(conn, result.document_id)) == 1


def test_links_are_always_recorded_as_manual(conn):
    """M4 never guesses; SPEC's automatic columns exist but stay unused."""
    result = archive.ingest(conn, FROM_GMAIL, source="paste")
    archive.link_course(conn, result.document_id, 1)

    row = conn.execute("SELECT created_by, confidence FROM document_links").fetchone()
    assert row["created_by"] == "manual"
    assert row["confidence"] == 1.0


# --- deleting ---------------------------------------------------------------


def test_deleting_takes_the_sources_links_and_index_with_it(conn):
    result = archive.ingest(conn, FROM_GMAIL, source="paste")
    archive.link_course(conn, result.document_id, 1)

    archive.delete(conn, result.document_id)

    assert archive.count(conn) == 0
    assert archive.sources_for(conn, result.document_id) == []
    assert conn.execute("SELECT COUNT(*) n FROM document_links").fetchone()["n"] == 0
    assert archive.search(conn, "notebook") == []


def test_a_deletion_is_audited(conn):
    result = archive.ingest(conn, FROM_GMAIL, source="paste")
    archive.delete(conn, result.document_id)

    actions = [
        row["action"]
        for row in conn.execute("SELECT action FROM audit_log ORDER BY id")
    ]
    assert "delete" in actions


def test_the_same_message_can_be_saved_again_after_deleting_it(conn):
    """The unique hash must not become a tombstone."""
    first = archive.ingest(conn, FROM_GMAIL, source="paste")
    archive.delete(conn, first.document_id)

    again = archive.ingest(conn, FROM_GMAIL, source="paste")
    assert again.created is True
