"""The verbatim document archive: saving, deduplicating and searching (SPEC §7).

Three ideas hold this module together.

**The body is never changed.** ``documents.body`` is whatever arrived, byte for
byte. Everything in here that looks like it edits a message — normalisation,
subject derivation, snippets — produces something *alongside* the body, never a
replacement for it. The database enforces that too, with a trigger.

**Deduplication happens before insert, on a normalised copy.** SPEC §7: a Canvas
conversation that also arrives by email is one document with two provenance rows.
The same message shared from Mail and pasted from Canvas is byte-different — the
quoting differs, one carries "Sent from my iPhone" — so the hash is taken of a
stripped-down version while the verbatim original is what gets stored.

**Retrieval is FTS5 and BM25, and nothing else.** No embeddings, no vector store.
SPEC §7 gives three reasons: Anthropic has no embeddings endpoint so vectors mean
a second vendor; the archive is curated rather than exhaustive, so signal-to-noise
is high and keywords work; and the whole corpus fits in a context window anyway.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import sqlite3
import unicodedata
from dataclasses import dataclass

log = logging.getLogger("archive")

# Big enough for any email anyone actually sends, small enough that a runaway
# Shortcut cannot fill a 1GB server's disk one POST at a time.
MAX_BODY_BYTES = 200_000

SOURCES = ("share_sheet", "paste", "mail_bridge", "gmail_poll")
KINDS = ("email", "canvas_message", "announcement", "note", "other")

# How many results a search returns. SPEC §7 puts BM25 top-30 comfortably inside
# the context window, and the same number is a sensible page for a human.
SEARCH_LIMIT = 30


class ArchiveError(ValueError):
    """A document could not be saved, for a reason worth showing the owner."""


# --- normalisation ----------------------------------------------------------
#
# Everything below decides whether two arrivals of the same message become one
# document or two. It is worth reading slowly; a mistake here is discovered as a
# duplicated archive months later, which is exactly what SPEC §7 says to avoid by
# building this in M4 rather than retrofitting it.

# A line of five or more underscores or dashes: Outlook's separator above a
# forwarded or quoted block.
_SEPARATOR_RE = re.compile(r"^\s*[_-]{5,}\s*$")

# "-----Original Message-----", with any number of dashes and any capitalisation.
_ORIGINAL_MESSAGE_RE = re.compile(r"^\s*-*\s*original message\s*-*\s*$", re.I)

# Gmail's "---------- Forwarded message ---------" and Apple Mail's "Begin
# forwarded message:". Both put text in the middle of the rule, so the plain
# separator pattern above does not catch them.
_FORWARDED_RE = re.compile(
    r"^\s*-*\s*(begin\s+)?forwarded message\s*:?\s*-*\s*$", re.I
)

# Gmail and Apple Mail: "On Mon, 1 Sep 2026 at 09:14, Ana Ruiz <a@iu.edu> wrote:".
# Long ones wrap, so the "wrote:" may land on the following line — handled by the
# caller, which is why this only anchors the start.
_ON_WROTE_START_RE = re.compile(r"^\s*On\b.*", re.I)
_WROTE_END_RE = re.compile(r"wrote:\s*$", re.I)

# The Outlook header block that introduces a quoted message. "From:" alone is not
# enough — plenty of ordinary messages open with one — so the caller requires a
# second header line within the next few.
_FROM_RE = re.compile(r"^\s*From:\s*\S", re.I)
_HEADER_RE = re.compile(r"^\s*(Sent|Date|To|Cc|Subject):\s*\S", re.I)

# RFC 3676's signature delimiter: a line consisting of exactly "--", optionally
# with one trailing space. Mail clients that honour it put nothing else on it.
_SIGNATURE_RE = re.compile(r"^--\s?$")

# The tails phones and clients append. Matched anywhere in the trailing few lines
# rather than only at the very end, because clients add their own line after them.
_APPENDED_RE = re.compile(
    r"^\s*(sent from my \w+"
    r"|sent via \w+"
    r"|get outlook for \w+"
    r"|download outlook for \w+)\s*[.!]?\s*$",
    re.I,
)

# Curly quotes, dashes and non-breaking spaces. Mail and Canvas disagree about
# these constantly, and a single smart apostrophe is enough to split one message
# into two documents. Unicode normalisation alone does not fold them, so they are
# mapped by hand.
_PUNCTUATION = str.maketrans(
    {
        "‘": "'", "’": "'", "‚": "'", "‛": "'",
        "“": '"', "”": '"', "„": '"', "‟": '"',
        "–": "-", "—": "-", "―": "-", "−": "-",
        "…": "...",
        " ": " ", " ": " ", " ": " ", "​": "", "﻿": "",
        "­": "",
    }
)


def _quote_starts_at(lines: list[str]) -> int | None:
    """The index of the first line belonging to a quoted reply chain."""
    for index, line in enumerate(lines):
        stripped = line.lstrip()

        if stripped.startswith(">"):
            return index
        if _ORIGINAL_MESSAGE_RE.match(line) or _FORWARDED_RE.match(line):
            return index

        # A separator rule only counts as a quote marker when a quoted block
        # actually follows it; on its own it is decoration.
        if _SEPARATOR_RE.match(line):
            following = lines[index + 1 : index + 4]
            if any(_FROM_RE.match(item) or _HEADER_RE.match(item) for item in following):
                return index

        if _ON_WROTE_START_RE.match(line):
            # "wrote:" on this line, or on one of the next two if it wrapped.
            window = " ".join(lines[index : index + 3])
            if _WROTE_END_RE.search(line) or _WROTE_END_RE.search(window):
                return index

        if _FROM_RE.match(line):
            following = lines[index + 1 : index + 5]
            if any(_HEADER_RE.match(item) for item in following):
                return index

    return None


def _strip_trailer(lines: list[str]) -> list[str]:
    """Remove the signature block and any client-appended tail."""
    for index, line in enumerate(lines):
        if _SIGNATURE_RE.match(line):
            return lines[:index]

    # No delimiter, so fall back to trimming the recognisable appended lines from
    # the end. Only from the end: "Sent from my iPhone" quoted in the middle of a
    # message is part of what was said.
    end = len(lines)
    while end > 0:
        candidate = lines[end - 1]
        if not candidate.strip() or _APPENDED_RE.match(candidate):
            end -= 1
        else:
            break
    return lines[:end]


# Header lines at the top of a forwarded block, which carry the routing rather
# than the message.
_ANY_HEADER_RE = re.compile(
    r"^\s*(From|Sent|Date|To|Cc|Bcc|Subject|Reply-To|Importance):\s", re.I
)


def _unwrap_forward(lines: list[str]) -> list[str]:
    """Pull the original message out of a forwarding wrapper.

    Auto-forwarding produces a body whose *entire* content sits below a quote
    marker: the separator, then a From/Sent/To block, then what was actually
    written. Treating that as "a reply with nothing above the quote" would leave
    nothing at all, and refusing it would make automatic collection useless.

    So the marker and its header block are dropped and the rest is kept. That has
    a second, better effect: a message forwarded automatically and the same
    message saved by hand from the phone reduce to the same text, which is what
    lets deduplication recognise them as one.
    """
    body = []
    seen_content = False
    for line in lines:
        if not seen_content:
            stripped = line.lstrip("> ").rstrip()
            if (
                not stripped
                or _SEPARATOR_RE.match(line)
                or _ORIGINAL_MESSAGE_RE.match(stripped)
                or _FORWARDED_RE.match(stripped)
                or _ANY_HEADER_RE.match(stripped)
                or _WROTE_END_RE.search(stripped)
            ):
                continue
            seen_content = True
        # Quoted forwards prefix every line; the prefix is routing, not content.
        body.append(re.sub(r"^\s*>+\s?", "", line))
    return body


def _content_lines(lines: list[str]) -> list[str]:
    """The lines that are the message itself."""
    cut = _quote_starts_at(lines)
    if cut is None:
        return _strip_trailer(lines)

    above = _strip_trailer(lines[:cut])
    if any(line.strip() for line in above):
        # An ordinary reply: what was written sits above what was quoted.
        return above

    return _strip_trailer(_unwrap_forward(lines[cut:]))


def normalize(body: str) -> str:
    """The comparable form of a message, used for hashing and never stored.

    Two arrivals of one message should produce one string here. Everything it
    throws away — the quoted chain below the reply, the signature, the client's
    "Sent from my iPhone", the difference between a curly and a straight
    apostrophe — is something that varies by route rather than by content.
    """
    text = unicodedata.normalize("NFKC", body).translate(_PUNCTUATION)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    lines = _content_lines(lines)

    # Case-folded and whitespace-collapsed. Two genuinely different messages do
    # not differ only in capitalisation or line wrapping, but two copies of one
    # message differ in both constantly.
    return " ".join(" ".join(lines).split()).casefold()


def body_hash(body: str) -> str:
    """SHA-256 of the normalised body — the identity of a message."""
    return hashlib.sha256(normalize(body).encode("utf-8")).hexdigest()


def derive_subject(body: str, limit: int = 120) -> str | None:
    """A subject taken from the message itself, for saves that supply none.

    Every share-sheet save lands here: iOS hands over body text and nothing else.
    The first real line is almost always the subject, the greeting, or the first
    sentence, all of which beat a list of rows reading "Untitled".
    """
    for line in body.replace("\r\n", "\n").split("\n"):
        stripped = " ".join(line.split())
        if stripped:
            return stripped[:limit]
    return None


# --- saving -----------------------------------------------------------------


@dataclass
class IngestResult:
    document_id: int
    #: False when the body matched a document already held, per SPEC §7.
    created: bool
    #: False when this exact provenance was already recorded.
    source_added: bool


def _audit(conn: sqlite3.Connection, action: str, document_id: int, detail: dict) -> None:
    conn.execute(
        "INSERT INTO audit_log (action, table_name, record_id, detail_json) "
        "VALUES (?, 'documents', ?, ?)",
        (action, document_id, json.dumps(detail)),
    )


def ingest(
    conn: sqlite3.Connection,
    body: str,
    *,
    source: str,
    subject: str | None = None,
    sender: str | None = None,
    received_at: str | None = None,
    kind: str = "other",
    external_id: str | None = None,
    raw_headers: str | None = None,
) -> IngestResult:
    """Save a message, or record another way an already-saved one arrived.

    The single insert point SPEC §7 asks for. The share sheet, the paste form and
    any future mail bridge are adapters over this function; none of them writes to
    ``documents`` itself.
    """
    if source not in SOURCES:
        raise ArchiveError(f"Unknown source {source!r}.")
    if kind not in KINDS:
        raise ArchiveError(f"Unknown kind {kind!r}.")

    if not body or not body.strip():
        raise ArchiveError("There is nothing to save — the message body is empty.")

    encoded = body.encode("utf-8")
    if len(encoded) > MAX_BODY_BYTES:
        raise ArchiveError(
            f"That message is {len(encoded) // 1024} KB, and the limit is "
            f"{MAX_BODY_BYTES // 1024} KB. Save the part that matters instead."
        )

    digest = body_hash(body)
    if not normalize(body):
        # Quoting and a signature and nothing else: there is no message here, and
        # storing it would give every such arrival the same hash.
        raise ArchiveError(
            "That looks like a quoted reply or a signature with no message of its "
            "own. Save the part that was actually written."
        )

    existing = conn.execute(
        "SELECT id FROM documents WHERE body_sha256 = ?", (digest,)
    ).fetchone()

    if existing is not None:
        document_id = int(existing["id"])
        added = _record_source(conn, document_id, source, external_id, raw_headers)
        if added:
            _audit(conn, "source_added", document_id, {"source": source})
        log.info("Document %d already held; source=%s added=%s", document_id, source, added)
        return IngestResult(document_id, created=False, source_added=added)

    cur = conn.execute(
        "INSERT INTO documents (body, body_sha256, subject, sender, received_at, kind) "
        "VALUES (?,?,?,?,?,?)",
        (
            body,
            digest,
            (subject or "").strip() or derive_subject(body),
            (sender or "").strip() or None,
            (received_at or "").strip() or None,
            kind,
        ),
    )
    document_id = int(cur.lastrowid)
    _record_source(conn, document_id, source, external_id, raw_headers)
    _audit(conn, "create", document_id, {"source": source, "kind": kind})
    log.info("Saved document %d from %s.", document_id, source)
    return IngestResult(document_id, created=True, source_added=True)


def _record_source(
    conn: sqlite3.Connection,
    document_id: int,
    source: str,
    external_id: str | None,
    raw_headers: str | None,
) -> bool:
    """Add a provenance row unless this exact one is already there."""
    already = conn.execute(
        "SELECT 1 FROM document_sources WHERE document_id = ? AND source = ? "
        "AND external_id IS ?",
        (document_id, source, external_id),
    ).fetchone()
    if already is not None:
        return False

    conn.execute(
        "INSERT INTO document_sources (document_id, source, external_id, raw_headers) "
        "VALUES (?,?,?,?)",
        (document_id, source, external_id, raw_headers),
    )
    return True


# --- searching --------------------------------------------------------------

# Sentinel characters FTS5 wraps matches in. They are control codes, so they
# cannot occur in a real message, which means the replacement below cannot be
# tricked by a body containing the literal text "<mark>".
_HIT_OPEN = "\x02"
_HIT_CLOSE = "\x03"

# Words shorter than this are matched exactly rather than as prefixes. "a*" would
# otherwise match the entire archive.
_PREFIX_MIN = 3


def match_expression(query: str) -> str | None:
    """Turn what was typed into something FTS5 will accept.

    FTS5's MATCH takes a query language, not a phrase: an unbalanced quote is a
    syntax error, and ``AND``, ``OR``, ``NOT``, ``NEAR`` and ``*`` are operators.
    Typing any of those into a search box is ordinary behaviour, so every word is
    extracted and re-quoted rather than passed through.

    Words get a prefix match, so "lab" finds "labs" and "laboratory" — worth a
    little noise on an archive of a few dozen messages.
    """
    words = re.findall(r"\w+", query, flags=re.UNICODE)
    if not words:
        return None
    return " ".join(
        f'"{word}"*' if len(word) >= _PREFIX_MIN else f'"{word}"' for word in words
    )


def snippet_html(snippet: str) -> str:
    """Escape a snippet, then turn FTS5's markers into <mark> tags.

    Order matters and is the whole point: the text is escaped first, so a message
    containing ``<script>`` renders as text, and only the sentinels the search
    engine inserted become markup.
    """
    escaped = html.escape(snippet)
    return escaped.replace(_HIT_OPEN, "<mark>").replace(_HIT_CLOSE, "</mark>")


_SUMMARY_COLUMNS = """
    d.id, d.subject, d.sender, d.received_at, d.ingested_at, d.kind
"""


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    course_id: int | None = None,
    limit: int = SEARCH_LIMIT,
) -> list[sqlite3.Row]:
    """BM25 search over subject and body, best first.

    The subject is weighted well above the body: a word in the subject line of a
    message is a much stronger signal than the same word buried in a paragraph.
    """
    expression = match_expression(query)
    if expression is None:
        return []

    sql = f"""
        SELECT {_SUMMARY_COLUMNS},
               snippet(documents_fts, 1, ?, ?, '…', 24) AS body_snippet
          FROM documents_fts
          JOIN documents d ON d.id = documents_fts.rowid
         WHERE documents_fts MATCH ?
    """
    params: list = [_HIT_OPEN, _HIT_CLOSE, expression]

    if course_id is not None:
        sql += """
           AND EXISTS (SELECT 1 FROM document_links l
                        WHERE l.document_id = d.id
                          AND l.target_type = 'course' AND l.target_id = ?)
        """
        params.append(course_id)

    # bm25() returns a negative score where more negative is a better match, so
    # ascending order puts the best first.
    sql += " ORDER BY bm25(documents_fts, 5.0, 1.0) LIMIT ?"
    params.append(limit)

    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        # match_expression should make this unreachable. If some input still gets
        # through, an empty result page beats a 500 on the page used for finding
        # things in a hurry.
        log.warning("FTS5 rejected a search: %s", exc)
        return []


def recent(conn: sqlite3.Connection, limit: int = SEARCH_LIMIT) -> list[sqlite3.Row]:
    """The newest documents, for a search page with nothing typed into it yet.

    Ordered by when the message was received where that is known, and by when it
    was saved otherwise — a share-sheet save has no received date, and sorting
    those to the bottom would hide exactly the things saved most recently.
    """
    return conn.execute(
        f"""
        SELECT {_SUMMARY_COLUMNS}, '' AS body_snippet
          FROM documents d
         ORDER BY COALESCE(d.received_at, d.ingested_at) DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()


def count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"])


# --- one document -----------------------------------------------------------


def get(conn: sqlite3.Connection, document_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM documents WHERE id = ?", (document_id,)
    ).fetchone()


def sources_for(conn: sqlite3.Connection, document_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT source, external_id, ingested_at FROM document_sources "
        "WHERE document_id = ? ORDER BY id",
        (document_id,),
    ).fetchall()


def courses_for(conn: sqlite3.Connection, document_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT c.id, c.name, c.code
          FROM document_links l
          JOIN courses c ON c.id = l.target_id
         WHERE l.document_id = ? AND l.target_type = 'course'
         ORDER BY c.name
        """,
        (document_id,),
    ).fetchall()


def courses_for_many(conn: sqlite3.Connection, document_ids: list[int]) -> dict[int, list]:
    """Course links for a page of search results, in one query rather than N."""
    if not document_ids:
        return {}
    placeholders = ",".join("?" * len(document_ids))
    rows = conn.execute(
        f"""
        SELECT l.document_id, c.id, c.name, c.code
          FROM document_links l
          JOIN courses c ON c.id = l.target_id
         WHERE l.target_type = 'course' AND l.document_id IN ({placeholders})
         ORDER BY c.name
        """,
        document_ids,
    ).fetchall()

    grouped: dict[int, list] = {}
    for row in rows:
        grouped.setdefault(row["document_id"], []).append(row)
    return grouped


def link_course(conn: sqlite3.Connection, document_id: int, course_id: int) -> None:
    """Attach a document to a course. Always manual — nothing here guesses."""
    conn.execute(
        "INSERT OR IGNORE INTO document_links "
        "(document_id, target_type, target_id, confidence, created_by) "
        "VALUES (?, 'course', ?, 1.0, 'manual')",
        (document_id, course_id),
    )
    _audit(conn, "link", document_id, {"course_id": course_id})


def unlink_course(conn: sqlite3.Connection, document_id: int, course_id: int) -> None:
    conn.execute(
        "DELETE FROM document_links WHERE document_id = ? AND target_type = 'course' "
        "AND target_id = ?",
        (document_id, course_id),
    )
    _audit(conn, "unlink", document_id, {"course_id": course_id})


def delete(conn: sqlite3.Connection, document_id: int) -> None:
    """Remove a document entirely.

    The body being immutable does not make the row permanent: a mis-paste, or a
    message saved from the wrong thread, has to be removable. Sources and links go
    with it by cascade, and the audit row recording the deletion stays.
    """
    _audit(conn, "delete", document_id, {})
    conn.execute("DELETE FROM document_sources WHERE document_id = ?", (document_id,))
    conn.execute("DELETE FROM document_links WHERE document_id = ?", (document_id,))
    conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
