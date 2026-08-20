"""Collecting forwarded mail over IMAP, into a review queue (SPEC §7).

SPEC §7 expected the automatic path to be a Gmail POP pull. That is unavailable —
the institution blocks forwarding out of the IU account and Gmail offers no pull —
so the route that works is the other direction: Outlook auto-forwards to a mailbox
used for nothing else, and this reads that mailbox.

**Nothing here writes to the archive.** Collected mail lands in
``inbound_messages`` and waits to be kept or discarded, for the reason set out in
migration 0007: SPEC §7's case for keyword search rests on the archive being
curated rather than exhaustive, and a whole university mail account is exhaustive.

Standard library only: ``imaplib`` and ``email``, the same reasoning that kept the
Canvas fetch and the CalDAV push off PyPI. This runs on 1 vCPU, and every
dependency is something that breaks on upgrade at a moment nobody chose.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import html
import imaplib
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser

from app import archive, db

log = logging.getLogger("mailbox")

# SPEC §6 polls Canvas every 30 minutes; mail is the same order of urgency. A
# forwarded message is not something anyone is waiting on to the minute.
POLL_INTERVAL_SECONDS = 15 * 60

# How many messages one poll will take. A first run against a mailbox with a
# term's backlog in it should not hold a connection open for minutes or build a
# review queue of four hundred items in one go.
MAX_PER_POLL = 40

# Anything larger is almost certainly a newsletter with an image gallery in it.
# The limit is on the extracted text, not the raw message with its attachments.
MAX_BODY_CHARS = 100_000

SOURCE = "mail_poll"

# The provenance value written when one of these is kept. SPEC §5 fixes the four
# permitted values in a CHECK constraint, and gmail_poll is the one that means
# "collected from a mailbox rather than saved by hand". The name is the spec's,
# from when the expected provider was Gmail; the mechanism is IMAP and the
# provider is whatever the forwarding points at.
ARCHIVE_SOURCE = "gmail_poll"


class MailboxError(RuntimeError):
    """The mailbox could not be read. Never carries the password."""


def configured() -> bool:
    return all(
        os.environ.get(name, "").strip()
        for name in ("MAIL_IMAP_HOST", "MAIL_USERNAME", "MAIL_PASSWORD")
    )


def settings() -> tuple[str, int, str, str, str]:
    return (
        os.environ.get("MAIL_IMAP_HOST", "").strip(),
        int(os.environ.get("MAIL_IMAP_PORT", "993").strip() or 993),
        os.environ.get("MAIL_USERNAME", "").strip(),
        os.environ.get("MAIL_PASSWORD", ""),
        os.environ.get("MAIL_FOLDER", "INBOX").strip() or "INBOX",
    )


# --- turning a message into text --------------------------------------------


class _TextExtractor(HTMLParser):
    """The smallest HTML-to-text that produces readable mail.

    Plenty of university mail is sent as HTML only. This is not a renderer and
    does not try to be: it drops the parts that are not prose and turns the tags
    that mean "new line" into new lines. Anything cleverer would be a dependency,
    and the result only has to be readable and searchable.
    """

    _SKIP = {"script", "style", "head", "title"}
    _BREAK = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skipping += 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skipping:
            self.parts.append(data)

    def text(self) -> str:
        joined = "".join(self.parts)
        # Collapse the runs of blank lines that come from nested block tags,
        # without joining paragraphs that were genuinely separate.
        return re.sub(r"\n{3,}", "\n\n", joined).strip()


def html_to_text(source: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(source)
        parser.close()
    except Exception:  # noqa: BLE001 — malformed mail must not stop a poll
        log.warning("Could not parse an HTML message body; falling back to a strip.")
        return html.unescape(re.sub(r"<[^>]+>", " ", source)).strip()
    return parser.text()


def body_text(message: email.message.Message) -> str:
    """The readable text of a message, preferring the plain-text part."""
    if not message.is_multipart():
        content = message.get_content() if hasattr(message, "get_content") else ""
        if message.get_content_type() == "text/html":
            return html_to_text(str(content))
        return str(content)

    plain: list[str] = []
    rich: list[str] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        # An attachment is not the message, even when it is a text file.
        if part.get_content_disposition() == "attachment":
            continue
        try:
            content = str(part.get_content())
        except Exception:  # noqa: BLE001 — an undecodable part is not fatal
            continue
        if part.get_content_type() == "text/plain":
            plain.append(content)
        elif part.get_content_type() == "text/html":
            rich.append(content)

    if plain:
        return "\n".join(plain).strip()
    if rich:
        return html_to_text("\n".join(rich))
    return ""


def _received_at(message: email.message.Message) -> str | None:
    raw = message.get("Date")
    if not raw:
        return None
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Which headers are worth keeping. The whole set would carry pages of routing and
# spam scoring per message; these are the ones a human would look at to work out
# where something came from.
KEPT_HEADERS = (
    "From", "To", "Cc", "Subject", "Date", "Message-ID",
    "Reply-To", "X-Original-Sender", "Delivered-To", "Return-Path",
)


def _headers(message: email.message.Message) -> str:
    lines = []
    for name in KEPT_HEADERS:
        for value in message.get_all(name, []):
            lines.append(f"{name}: {value}")
    return "\n".join(lines)


@dataclass
class Collected:
    uid: str
    message_id: str | None
    subject: str | None
    sender: str | None
    received_at: str | None
    body: str
    raw_headers: str


def parse(uid: str, raw: bytes) -> Collected | None:
    """Turn one fetched message into something storable, or None if it is empty."""
    message = email.message_from_bytes(raw, policy=email.policy.default)

    text = body_text(message)[:MAX_BODY_CHARS].strip()
    if not text:
        return None

    subject = message.get("Subject")
    return Collected(
        uid=uid,
        message_id=(message.get("Message-ID") or "").strip() or None,
        subject=str(subject).strip()[:300] if subject else None,
        sender=(str(message.get("From")) or "").strip()[:300] or None,
        received_at=_received_at(message),
        body=text,
        raw_headers=_headers(message),
    )


# --- reading the mailbox ----------------------------------------------------


def _cursor(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT cursor FROM sync_state WHERE source = ?", (SOURCE,)
    ).fetchone()
    return row["cursor"] if row else None


def _record(conn: sqlite3.Connection, *, ok: bool, error: str | None,
            cursor: str | None = None) -> None:
    """Write the outcome of an attempt, so a poll that stopped becomes visible.

    SPEC §11 forbids secrets in error messages, and sync_state.last_error is
    rendered in the browser. Callers pass a stage, never an exception whose text
    could quote the password.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO sync_state (source, last_attempt_at) VALUES (?, ?) "
        "ON CONFLICT(source) DO UPDATE SET last_attempt_at = excluded.last_attempt_at",
        (SOURCE, now),
    )
    if ok:
        conn.execute(
            "UPDATE sync_state SET last_success_at = ?, last_error = NULL, "
            "consecutive_failures = 0, cursor = COALESCE(?, cursor) WHERE source = ?",
            (now, cursor, SOURCE),
        )
    else:
        conn.execute(
            "UPDATE sync_state SET last_error = ?, "
            "consecutive_failures = consecutive_failures + 1 WHERE source = ?",
            (error, SOURCE),
        )


@dataclass
class PollResult:
    fetched: int = 0
    queued: int = 0
    #: Already in the archive, so recorded as another route and never queued.
    already_held: int = 0
    #: Seen before and decided on already.
    skipped: int = 0


def _connect() -> imaplib.IMAP4_SSL:
    host, port, username, password, _ = settings()
    try:
        client = imaplib.IMAP4_SSL(host, port, timeout=30)
    except Exception as exc:  # noqa: BLE001
        raise MailboxError(
            f"Could not reach the mail server at {host}:{port}. "
            f"({type(exc).__name__})"
        ) from None

    try:
        client.login(username, password)
    except imaplib.IMAP4.error:
        # The server's message can quote the credentials it rejected, so it is
        # deliberately not included.
        raise MailboxError(
            "The mail server refused the sign-in. Check MAIL_USERNAME, and that "
            "MAIL_PASSWORD is an app-specific password rather than the account "
            "password."
        ) from None
    return client


def poll(conn: sqlite3.Connection) -> PollResult:
    """Read new mail into the review queue. Never writes to `documents` directly."""
    if not configured():
        raise MailboxError(
            "Mail collection is not switched on: MAIL_IMAP_HOST, MAIL_USERNAME and "
            "MAIL_PASSWORD are not all set. See docs/SETUP.md section 18."
        )

    _, _, _, _, folder = settings()
    client = _connect()
    try:
        # Read-only: this must not change what is unread in a mailbox somebody
        # may also be looking at.
        status, _ = client.select(f'"{folder}"', readonly=True)
        if status != "OK":
            raise MailboxError(
                f"The mailbox has no folder called {folder!r}. Check MAIL_FOLDER."
            )

        validity = client.response("UIDVALIDITY")[1][0]
        validity = validity.decode() if isinstance(validity, bytes) else str(validity)

        cursor = _cursor(conn)
        start = 1
        if cursor and cursor.startswith(f"{validity}:"):
            start = int(cursor.split(":", 1)[1]) + 1
        elif cursor:
            # The mailbox was rebuilt and UIDs restarted. Reading from 1 again is
            # safe: everything already seen is recognised by hash or by the
            # unique index, so the queue does not fill with what was decided on.
            log.warning("The mailbox was rebuilt; reading it from the beginning.")

        status, data = client.uid("SEARCH", None, f"{start}:*")
        if status != "OK":
            raise MailboxError("The mail server rejected the search for new mail.")

        uids = [item.decode() for item in (data[0] or b"").split()]
        # IMAP answers `n:*` with the last message even when nothing is that new.
        uids = [uid for uid in uids if int(uid) >= start][:MAX_PER_POLL]

        result = PollResult()
        highest = start - 1

        for uid in uids:
            status, payload = client.uid("FETCH", uid, "(RFC822)")
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                log.warning("Could not fetch message %s; skipping it.", uid)
                continue

            result.fetched += 1
            highest = max(highest, int(uid))
            _store(conn, f"{validity}:{uid}", payload[0][1], result)

        cursor = f"{validity}:{highest}" if highest >= start - 1 else None
        _record(conn, ok=True, error=None, cursor=cursor)
        log.info(
            "Mail poll: %d fetched, %d queued, %d already held, %d skipped.",
            result.fetched, result.queued, result.already_held, result.skipped,
        )
        return result
    finally:
        try:
            client.logout()
        except Exception:  # noqa: BLE001 — a failed logout is not a failed poll
            pass


def _store(conn: sqlite3.Connection, uid: str, raw: bytes, result: PollResult) -> None:
    """Queue one message, unless it is already known."""
    seen = conn.execute(
        "SELECT 1 FROM inbound_messages WHERE uid = ?", (uid,)
    ).fetchone()
    if seen is not None:
        result.skipped += 1
        return

    collected = parse(uid, raw)
    if collected is None:
        result.skipped += 1
        return

    digest = archive.body_hash(collected.body)
    if not archive.normalize(collected.body):
        result.skipped += 1
        return

    held = conn.execute(
        "SELECT id FROM documents WHERE body_sha256 = ?", (digest,)
    ).fetchone()

    if held is not None:
        # Already saved by hand. Record that it also arrived this way and do not
        # ask about it — being asked to review something already in the archive
        # is how a review queue teaches you to ignore it.
        archive.ingest(
            conn,
            collected.body,
            source=ARCHIVE_SOURCE,
            subject=collected.subject,
            sender=collected.sender,
            received_at=collected.received_at,
            kind="email",
            external_id=collected.message_id,
            raw_headers=collected.raw_headers,
        )
        conn.execute(
            "INSERT INTO inbound_messages (uid, message_id, body_sha256, subject, "
            "sender, received_at, body, raw_headers, state, document_id, decided_at) "
            "VALUES (?,?,?,?,?,?,?,?, 'kept', ?, "
            "strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
            (uid, collected.message_id, digest, collected.subject, collected.sender,
             collected.received_at, collected.body, collected.raw_headers, held["id"]),
        )
        result.already_held += 1
        return

    # Two forwards of one message in the same batch: queue it once.
    queued = conn.execute(
        "SELECT 1 FROM inbound_messages WHERE body_sha256 = ? AND state <> 'discarded'",
        (digest,),
    ).fetchone()
    if queued is not None:
        result.skipped += 1
        return

    conn.execute(
        "INSERT INTO inbound_messages (uid, message_id, body_sha256, subject, sender, "
        "received_at, body, raw_headers) VALUES (?,?,?,?,?,?,?,?)",
        (uid, collected.message_id, digest, collected.subject, collected.sender,
         collected.received_at, collected.body, collected.raw_headers),
    )
    result.queued += 1


def sync(conn: sqlite3.Connection) -> PollResult:
    """One poll, with failures recorded rather than raised at the scheduler."""
    try:
        return poll(conn)
    except MailboxError as exc:
        _record(conn, ok=False, error=str(exc))
        raise
    except Exception:
        _record(conn, ok=False, error="The mail poll failed unexpectedly.")
        raise


# --- the queue --------------------------------------------------------------


def pending(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM inbound_messages WHERE state = 'pending' "
        "ORDER BY COALESCE(received_at, fetched_at) DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def pending_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM inbound_messages WHERE state = 'pending'"
        ).fetchone()["n"]
    )


def keep(conn: sqlite3.Connection, inbound_id: int) -> int:
    """Move one queued message into the archive, and return its document id."""
    row = conn.execute(
        "SELECT * FROM inbound_messages WHERE id = ? AND state = 'pending'",
        (inbound_id,),
    ).fetchone()
    if row is None:
        raise MailboxError("That message is not waiting for review.")

    result = archive.ingest(
        conn,
        row["body"],
        source=ARCHIVE_SOURCE,
        subject=row["subject"],
        sender=row["sender"],
        received_at=row["received_at"],
        kind="email",
        external_id=row["message_id"],
        raw_headers=row["raw_headers"],
    )
    conn.execute(
        "UPDATE inbound_messages SET state = 'kept', document_id = ?, "
        "decided_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?",
        (result.document_id, inbound_id),
    )
    return result.document_id


def discard(conn: sqlite3.Connection, inbound_id: int) -> None:
    """Say no to one queued message.

    The row stays, in the `discarded` state. Deleting it would mean the next poll
    saw the message as new and asked again, which is the fastest way to make a
    review queue something you stop reading.
    """
    conn.execute(
        "UPDATE inbound_messages SET state = 'discarded', "
        "decided_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
        "WHERE id = ? AND state = 'pending'",
        (inbound_id,),
    )


def discard_all(conn: sqlite3.Connection) -> int:
    """Clear the queue in one go, for the backlog after a quiet fortnight."""
    count = pending_count(conn)
    conn.execute(
        "UPDATE inbound_messages SET state = 'discarded', "
        "decided_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE state = 'pending'"
    )
    return count


# --- the diagnostic ---------------------------------------------------------


def probe() -> int:
    """Connect, list the folders, and report — writing nothing.

    The same reasoning as the CalDAV probe: this cannot be tested from a laptop
    with no mailbox, so the failure has to arrive as a sentence about which step
    failed rather than as "mail collection does not work".
    """
    if not configured():
        print("Mail collection is not switched on.")
        print("Set MAIL_IMAP_HOST, MAIL_USERNAME and MAIL_PASSWORD in the secrets")
        print("file — see docs/SETUP.md section 18.")
        return 1

    host, port, username, _, folder = settings()
    print(f"Mail server:  {host}:{port}")
    print(f"Signing in as: {username}")
    print()

    try:
        print("  step 1  connecting and signing in")
        client = _connect()
        print("  step 2  signed in")

        status, folders = client.list()
        print(f"  step 3  {len(folders or [])} folder(s) on the account:")
        for item in (folders or [])[:25]:
            name = item.decode(errors="replace") if isinstance(item, bytes) else str(item)
            print(f"          {name.rsplit(' ', 1)[-1].strip(chr(34))}")

        status, _ = client.select(f'"{folder}"', readonly=True)
        if status != "OK":
            print()
            print(f"  step 4  FAILED — no folder called {folder!r}.")
            print("          Set MAIL_FOLDER to one of the names listed above.")
            client.logout()
            return 1

        total = client.response("EXISTS")[1][0]
        total = total.decode() if isinstance(total, bytes) else total
        print(f"  step 4  reading {folder!r}: {total} message(s) in it")
        print()
        print(f"  Mail would be collected from: {folder}")
        print("  Nothing has been read or changed by this check.")
        client.logout()
        return 0
    except MailboxError as exc:
        print()
        print(f"  FAILED: {exc}")
        return 1


def main() -> int:
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if "--probe" in sys.argv:
        return probe()

    conn = db.connect()
    try:
        result = sync(conn)
    except MailboxError as exc:
        print(f"Mail poll failed: {exc}")
        return 1
    finally:
        conn.close()

    print(
        f"{result.fetched} fetched, {result.queued} waiting for review, "
        f"{result.already_held} already held, {result.skipped} skipped."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
