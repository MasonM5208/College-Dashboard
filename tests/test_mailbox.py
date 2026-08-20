"""Tests for collecting forwarded mail (SPEC §7's automatic path).

There is no mailbox on this machine, so these drive a fake IMAP server built from
real message shapes. That covers parsing, the UID cursor, deduplication against
what is already archived, and the review queue — and covers none of whether the
provider's IMAP behaves as documented. Only the first real poll on the server
settles that, which is what `python -m app.mailbox --probe` exists for.
"""

from __future__ import annotations

import email.message
import email.utils
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import archive, config, db, mailbox, migrate
from app.main import app


# --- a fake IMAP server -----------------------------------------------------


class FakeIMAP:
    """Enough of imaplib.IMAP4_SSL to drive a poll.

    UIDs are the index in `messages`, one-based, which is how a real server
    numbers a mailbox nothing has ever been deleted from.
    """

    def __init__(self, messages, uidvalidity=b"111", folders=None):
        self.messages = list(messages)
        self.uidvalidity = uidvalidity
        self.folders = folders or [b'(\\HasNoChildren) "/" "INBOX"']
        self.logged_in = False
        self.selected = None
        self.readonly = None
        self.logged_out = False

    def login(self, username, password):
        self.logged_in = True
        return "OK", [b"authenticated"]

    def list(self):
        return "OK", self.folders

    def select(self, folder, readonly=False):
        name = folder.strip('"')
        if name not in [f.decode().rsplit(" ", 1)[-1].strip('"') for f in self.folders]:
            return "NO", [b"no such mailbox"]
        self.selected = name
        self.readonly = readonly
        return "OK", [str(len(self.messages)).encode()]

    def response(self, key):
        if key == "UIDVALIDITY":
            return key, [self.uidvalidity]
        if key == "EXISTS":
            return key, [str(len(self.messages)).encode()]
        return key, [None]

    def uid(self, command, *args):
        if command == "SEARCH":
            _, criteria = args
            start = int(criteria.split(":", 1)[0])
            found = [
                str(index + 1).encode()
                for index in range(len(self.messages))
                if index + 1 >= start
            ]
            # A real server answers n:* with the last message even when it is
            # older than n. The poller has to filter that out itself.
            if not found and self.messages:
                found = [str(len(self.messages)).encode()]
            return "OK", [b" ".join(found)]

        if command == "FETCH":
            uid = int(args[0])
            if uid > len(self.messages):
                return "NO", [None]
            return "OK", [(b"1 (RFC822 {...}", self.messages[uid - 1]), b")"]

        raise AssertionError(f"unexpected IMAP command {command}")

    def logout(self):
        self.logged_out = True
        return "BYE", [b"logging out"]


def make_message(body, subject="Lab 3", sender="Ana Ruiz <aruiz@iu.edu>",
                 message_id="<abc@mail.iu.edu>", html_body=None,
                 when=datetime(2026, 9, 1, 13, 14, tzinfo=timezone.utc)):
    message = email.message.EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = "semester@example.com"
    message["Message-ID"] = message_id
    message["Date"] = email.utils.format_datetime(when)
    message.set_content(body)
    if html_body is not None:
        message.add_alternative(html_body, subtype="html")
    return message.as_bytes()


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "TZ", "America/Indiana/Indianapolis")
    monkeypatch.setenv("MAIL_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("MAIL_USERNAME", "semester@example.com")
    monkeypatch.setenv("MAIL_PASSWORD", "app-specific-not-a-real-secret")
    migrate.run(path)
    connection = db.connect(path)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def server(monkeypatch):
    """Install a fake IMAP server and hand it back so tests can reconfigure it."""
    holder = {}

    def make(*args, **kwargs):
        return holder["server"]

    monkeypatch.setattr(mailbox.imaplib, "IMAP4_SSL", make)

    def install(messages, **kwargs):
        holder["server"] = FakeIMAP(messages, **kwargs)
        return holder["server"]

    return install


LAB = "Hi Mason,\n\nLab 3 has moved to Friday. Bring the notebook.\n\nAna\n"


# --- reading a message ------------------------------------------------------


def test_a_plain_message_is_read(conn, server):
    server([make_message(LAB)])

    result = mailbox.poll(conn)

    assert result.fetched == 1
    assert result.queued == 1
    row = mailbox.pending(conn)[0]
    assert row["subject"] == "Lab 3"
    assert "Bring the notebook" in row["body"]
    assert row["sender"] == "Ana Ruiz <aruiz@iu.edu>"
    assert row["received_at"] == "2026-09-01T13:14:00Z"
    assert row["message_id"] == "<abc@mail.iu.edu>"


def test_the_plain_part_is_preferred_over_the_html_one(conn, server):
    server([make_message(LAB, html_body="<p>Something <b>else</b> entirely</p>")])

    mailbox.poll(conn)

    body = mailbox.pending(conn)[0]["body"]
    assert "Bring the notebook" in body
    assert "else" not in body


def test_an_html_only_message_becomes_readable_text():
    """Plenty of university mail has no plain-text part at all."""
    html = (
        "<html><head><style>p{color:red}</style></head><body>"
        "<p>Lab 3 has moved to <b>Friday</b>.</p>"
        "<p>Bring the notebook &amp; a pen.</p>"
        "<script>alert(1)</script>"
        "</body></html>"
    )
    text = mailbox.html_to_text(html)

    assert "Lab 3 has moved to Friday." in text
    assert "Bring the notebook & a pen." in text
    assert "alert(1)" not in text
    assert "color:red" not in text
    assert "<" not in text


def test_useful_headers_are_kept_and_the_rest_are_not(conn, server):
    server([make_message(LAB)])
    mailbox.poll(conn)

    headers = mailbox.pending(conn)[0]["raw_headers"]

    assert "From: Ana Ruiz <aruiz@iu.edu>" in headers
    assert "Message-ID: <abc@mail.iu.edu>" in headers
    assert "Content-Type" not in headers


def test_a_message_with_no_readable_text_is_skipped(conn, server):
    empty = email.message.EmailMessage()
    empty["Subject"] = "Nothing"
    empty["From"] = "someone@example.com"
    empty.set_content("   \n\n")
    server([empty.as_bytes()])

    result = mailbox.poll(conn)

    assert result.queued == 0
    assert result.skipped == 1


# --- the queue --------------------------------------------------------------


def test_nothing_reaches_the_archive_without_being_kept(conn, server):
    """The whole point of the queue."""
    server([make_message(LAB)])

    mailbox.poll(conn)

    assert archive.count(conn) == 0
    assert mailbox.pending_count(conn) == 1


def test_keeping_a_message_puts_it_in_the_archive_verbatim(conn, server):
    server([make_message(LAB)])
    mailbox.poll(conn)
    queued = mailbox.pending(conn)[0]

    document_id = mailbox.keep(conn, queued["id"])

    document = archive.get(conn, document_id)
    assert document["body"] == queued["body"]
    assert document["kind"] == "email"
    assert archive.sources_for(conn, document_id)[0]["source"] == "gmail_poll"
    assert mailbox.pending_count(conn) == 0


def test_discarding_a_message_keeps_it_out_and_does_not_offer_it_again(conn, server):
    fake = server([make_message(LAB)])
    mailbox.poll(conn)
    mailbox.discard(conn, mailbox.pending(conn)[0]["id"])

    # A second poll of the same mailbox, cursor reset as if it had been rebuilt.
    conn.execute("UPDATE sync_state SET cursor = NULL WHERE source = 'mail_poll'")
    mailbox.poll(conn)

    assert mailbox.pending_count(conn) == 0
    assert archive.count(conn) == 0


def test_the_whole_queue_can_be_cleared_at_once(conn, server):
    server([make_message(f"Message number {n}.", message_id=f"<{n}@iu.edu>")
            for n in range(5)])
    mailbox.poll(conn)
    assert mailbox.pending_count(conn) == 5

    assert mailbox.discard_all(conn) == 5
    assert mailbox.pending_count(conn) == 0


def test_keeping_something_twice_is_refused_rather_than_duplicated(conn, server):
    server([make_message(LAB)])
    mailbox.poll(conn)
    queued = mailbox.pending(conn)[0]["id"]
    mailbox.keep(conn, queued)

    with pytest.raises(mailbox.MailboxError):
        mailbox.keep(conn, queued)


# --- deduplication ----------------------------------------------------------


def test_something_already_saved_by_hand_is_not_offered_for_review(conn, server):
    """Being asked about something already in the archive teaches you to ignore
    the queue, which is the one thing that would make it useless."""
    saved = archive.ingest(conn, LAB + "\nSent from my iPhone\n", source="share_sheet")
    server([make_message(LAB)])

    result = mailbox.poll(conn)

    assert result.already_held == 1
    assert mailbox.pending_count(conn) == 0
    assert archive.count(conn) == 1
    sources = sorted(row["source"] for row in archive.sources_for(conn, saved.document_id))
    assert sources == ["gmail_poll", "share_sheet"]


def test_a_forwarded_copy_of_something_saved_by_hand_is_recognised(conn, server):
    """Auto-forwarding wraps the message; the fingerprint has to see through it."""
    saved = archive.ingest(conn, LAB, source="share_sheet")
    forwarded = (
        "-----Original Message-----\n"
        "From: Ana Ruiz <aruiz@iu.edu>\n"
        "Sent: Monday, September 1, 2026 9:14 AM\n"
        "To: Mason Miller\n"
        "Subject: Lab 3\n\n"
    ) + LAB
    server([make_message(forwarded)])

    result = mailbox.poll(conn)

    assert result.already_held == 1
    assert archive.count(conn) == 1
    assert len(archive.sources_for(conn, saved.document_id)) == 2


def test_the_same_message_forwarded_twice_is_queued_once(conn, server):
    server([make_message(LAB, message_id="<a@iu.edu>"),
            make_message(LAB + "\nSent from my iPhone\n", message_id="<b@iu.edu>")])

    result = mailbox.poll(conn)

    assert result.fetched == 2
    assert mailbox.pending_count(conn) == 1


# --- the cursor -------------------------------------------------------------


def test_a_second_poll_does_not_re_read_what_it_already_has(conn, server):
    fake = server([make_message(LAB, message_id="<1@iu.edu>")])
    mailbox.poll(conn)

    fake.messages.append(make_message("Something new entirely.", message_id="<2@iu.edu>"))
    result = mailbox.poll(conn)

    assert result.fetched == 1
    assert mailbox.pending_count(conn) == 2


def test_a_poll_with_nothing_new_fetches_nothing(conn, server):
    server([make_message(LAB)])
    mailbox.poll(conn)

    assert mailbox.poll(conn).fetched == 0


def test_a_rebuilt_mailbox_is_read_from_the_start_without_re_queuing(conn, server):
    """UIDs restart from 1, so the cursor is meaningless and must be discarded.

    Reading everything again is safe only because the hash check catches what has
    already been decided on — which is what this asserts.
    """
    fake = server([make_message(LAB)])
    mailbox.poll(conn)
    mailbox.keep(conn, mailbox.pending(conn)[0]["id"])

    fake.uidvalidity = b"222"
    result = mailbox.poll(conn)

    assert result.fetched == 1
    assert result.already_held == 1
    assert archive.count(conn) == 1
    assert mailbox.pending_count(conn) == 0


def test_only_a_limited_number_are_taken_in_one_poll(conn, server):
    server([make_message(f"Message number {n}.", message_id=f"<{n}@iu.edu>")
            for n in range(mailbox.MAX_PER_POLL + 10)])

    result = mailbox.poll(conn)

    assert result.fetched == mailbox.MAX_PER_POLL


# --- failure ----------------------------------------------------------------


def test_the_mailbox_is_opened_read_only(conn, server):
    """Collecting mail must not mark it read in a mailbox someone else is using."""
    fake = server([make_message(LAB)])
    mailbox.poll(conn)
    assert fake.readonly is True


def test_a_missing_folder_says_which_setting_is_wrong(conn, server, monkeypatch):
    server([make_message(LAB)])
    monkeypatch.setenv("MAIL_FOLDER", "Semester")

    with pytest.raises(mailbox.MailboxError, match="MAIL_FOLDER"):
        mailbox.poll(conn)


def test_a_refused_sign_in_never_quotes_the_password(conn, server, monkeypatch):
    fake = server([])

    def refuse(username, password):
        raise mailbox.imaplib.IMAP4.error(
            f"AUTHENTICATIONFAILED for {username} with {password}"
        )

    monkeypatch.setattr(fake, "login", refuse)

    with pytest.raises(mailbox.MailboxError) as caught:
        mailbox.poll(conn)

    message = str(caught.value)
    assert "app-specific" in message
    assert "app-specific-not-a-real-secret" not in message
    assert "MAIL_PASSWORD" in message


def test_a_failure_is_recorded_without_the_password(conn, server, monkeypatch):
    fake = server([])
    monkeypatch.setattr(fake, "login", lambda u, p: (_ for _ in ()).throw(
        mailbox.imaplib.IMAP4.error(f"bad login {p}")
    ))

    with pytest.raises(mailbox.MailboxError):
        mailbox.sync(conn)

    row = conn.execute(
        "SELECT last_error, consecutive_failures FROM sync_state WHERE source = 'mail_poll'"
    ).fetchone()
    assert row["consecutive_failures"] == 1
    assert "app-specific-not-a-real-secret" not in row["last_error"]


def test_an_unconfigured_mailbox_says_so_rather_than_connecting(conn, monkeypatch):
    monkeypatch.delenv("MAIL_PASSWORD", raising=False)

    with pytest.raises(mailbox.MailboxError, match="MAIL_PASSWORD"):
        mailbox.poll(conn)


def test_a_successful_poll_is_recorded(conn, server):
    server([make_message(LAB)])
    mailbox.poll(conn)

    row = conn.execute(
        "SELECT last_success_at, last_error, cursor FROM sync_state "
        "WHERE source = 'mail_poll'"
    ).fetchone()
    assert row["last_success_at"] is not None
    assert row["last_error"] is None
    assert row["cursor"] == "111:1"


# --- the pages --------------------------------------------------------------


@pytest.fixture
def client(conn):
    with TestClient(app) as test_client:
        yield test_client


def test_the_review_page_lists_what_is_waiting(client, conn, server):
    server([make_message(LAB)])
    mailbox.poll(conn)

    body = client.get("/archive/review").text

    assert "Lab 3" in body
    assert "Bring the notebook" in body
    assert "Keep" in body


def test_keeping_from_the_page(client, conn, server):
    server([make_message(LAB)])
    mailbox.poll(conn)
    queued = mailbox.pending(conn)[0]["id"]

    client.post(f"/archive/review/{queued}/keep")

    assert archive.count(conn) == 1
    assert mailbox.pending_count(conn) == 0


def test_discarding_from_the_page(client, conn, server):
    server([make_message(LAB)])
    mailbox.poll(conn)
    queued = mailbox.pending(conn)[0]["id"]

    client.post(f"/archive/review/{queued}/discard")

    assert archive.count(conn) == 0
    assert mailbox.pending_count(conn) == 0


def test_the_archive_page_says_how_many_are_waiting(client, conn, server):
    server([make_message(LAB)])
    mailbox.poll(conn)

    body = client.get("/archive").text

    assert "1 forwarded message waiting" in body
    assert "/archive/review" in body


def test_the_review_page_is_calm_when_nothing_is_waiting(client):
    assert "Nothing waiting" in client.get("/archive/review").text
