-- 0006_documents.sql — the verbatim message archive (M4). From SPEC.md §5 and §7.
--
-- This is the table the whole project's trustworthiness rests on. SPEC §5:
-- "documents.body is immutable. Never updated, never summarized in place, never
-- truncated. This table is the permanent verbatim archive and the reason the
-- whole system is trustworthy."
--
-- The chat may paraphrase what is in here. Nothing may rewrite what is in here.


CREATE TABLE documents (
  id           INTEGER PRIMARY KEY,

  -- Exactly as it arrived. Quoted chains, signatures, stray whitespace and all.
  -- Nothing in the application ever updates this column, and the trigger below
  -- makes that a property of the database rather than a promise about the code.
  body         TEXT NOT NULL,

  -- SHA-256 of the NORMALIZED body, not of the column above. That distinction is
  -- the whole dedup design (SPEC §7): the same message shared from Mail and
  -- pasted from Canvas is byte-different — different quoting, different trailing
  -- whitespace, one has "Sent from my iPhone" — but normalizes to the same text
  -- and therefore to the same hash. Hashing the verbatim body instead would file
  -- two copies of everything that arrives twice.
  --
  -- UNIQUE so that a bug in app/archive.py cannot quietly create the duplicate
  -- the normalization was there to prevent.
  body_sha256  TEXT NOT NULL UNIQUE,

  subject      TEXT,
  sender       TEXT,

  -- When the message was sent or received, as opposed to when it was saved here.
  -- Nullable: an iOS share sheet hands over body text and nothing else, so most
  -- share-sheet saves know only when they were captured.
  received_at  TEXT,
  ingested_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),

  -- SPEC §5 names the column but not its values. These are the kinds that
  -- actually arrive; anything unclassified is 'other' rather than NULL, so the
  -- filter never has to reason about a missing value.
  kind         TEXT NOT NULL DEFAULT 'other'
               CHECK (kind IN ('email','canvas_message','announcement','note','other'))
);

CREATE INDEX documents_received_idx ON documents(received_at DESC);
CREATE INDEX documents_ingested_idx ON documents(ingested_at DESC);


-- SPEC §5 asks for immutability to be enforced in the application and documented
-- here. It is enforced in both places. The application is where the useful error
-- message lives; this is what still holds in M6, in M7, and at a sqlite3 prompt
-- at 1am.
--
-- Everything else about a document stays editable: a subject can be corrected, a
-- sender filled in, a kind reclassified. Only the record of what was said is
-- frozen.
CREATE TRIGGER documents_body_is_immutable
BEFORE UPDATE OF body, body_sha256 ON documents
BEGIN
  SELECT RAISE(ABORT, 'documents.body is the permanent verbatim archive and cannot be changed. Save a new document instead.');
END;


-- Where a document came from. MANY ROWS PER DOCUMENT (SPEC §5): a Canvas
-- conversation that also arrives by email is one document with two provenance
-- rows, which is what dedup produces rather than a second copy.
CREATE TABLE document_sources (
  id           INTEGER PRIMARY KEY,
  document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,

  -- All four of SPEC §7's paths. Only two are reachable today — the institution
  -- blocks forwarding and Gmail offers no POP pull — but they are listed anyway,
  -- so adding the Mac Mail bridge later is an INSERT and not a migration that
  -- rewrites a CHECK constraint on a populated table.
  source       TEXT NOT NULL
               CHECK (source IN ('share_sheet','paste','mail_bridge','gmail_poll')),

  -- The sending system's own identifier, when there is one: a Message-ID, a
  -- Canvas conversation id. Lets a re-poll recognise what it already has without
  -- reading the body.
  external_id  TEXT,
  raw_headers  TEXT,

  ingested_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX document_sources_document_idx ON document_sources(document_id);

-- Partial, because external_id is usually NULL and NULLs are not equal to each
-- other in SQLite — without the WHERE clause this index would still work, but
-- stating the intent is worth the clause.
CREATE UNIQUE INDEX document_sources_external_idx
  ON document_sources(source, external_id) WHERE external_id IS NOT NULL;


-- What a document is about. M4 only ever writes created_by='manual' with
-- confidence 1.0, because guessing which course an email belongs to is exactly
-- the kind of inference this archive exists to avoid. The columns for automatic
-- linking are here regardless: adding a column to a populated table later is the
-- destructive ALTER that CLAUDE.md says to ask before doing.
CREATE TABLE document_links (
  id           INTEGER PRIMARY KEY,
  document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  target_type  TEXT NOT NULL CHECK (target_type IN ('course','assignment')),
  target_id    INTEGER NOT NULL,
  confidence   REAL NOT NULL DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1),
  created_by   TEXT NOT NULL DEFAULT 'manual' CHECK (created_by IN ('auto','manual')),
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE UNIQUE INDEX document_links_unique_idx
  ON document_links(document_id, target_type, target_id);
CREATE INDEX document_links_target_idx ON document_links(target_type, target_id);


-- SPEC §7: FTS5 and BM25, no embeddings and no vector database. content= makes
-- this an external-content index: it stores the search structures but not a
-- second copy of every message body, which matters on a server with 1GB of RAM.
--
-- unicode61 with remove_diacritics 2 is the modern default; it means a search for
-- "resume" also finds "résumé", which is the behaviour anyone typing on a phone
-- keyboard expects.
CREATE VIRTUAL TABLE documents_fts USING fts5(
  subject,
  body,
  content='documents',
  content_rowid='id',
  tokenize="unicode61 remove_diacritics 2"
);


-- The three triggers SPEC §5 requires. External-content tables are not updated
-- automatically; deletes are recorded by re-inserting the old values under the
-- 'delete' command, which is the documented FTS5 idiom and looks stranger than it
-- is.
CREATE TRIGGER documents_fts_insert AFTER INSERT ON documents BEGIN
  INSERT INTO documents_fts (rowid, subject, body)
  VALUES (NEW.id, NEW.subject, NEW.body);
END;

CREATE TRIGGER documents_fts_delete AFTER DELETE ON documents BEGIN
  INSERT INTO documents_fts (documents_fts, rowid, subject, body)
  VALUES ('delete', OLD.id, OLD.subject, OLD.body);
END;

-- The body cannot change, but the subject can be corrected, and a correction that
-- did not reach the index would make a document unfindable by its own title.
CREATE TRIGGER documents_fts_update AFTER UPDATE ON documents BEGIN
  INSERT INTO documents_fts (documents_fts, rowid, subject, body)
  VALUES ('delete', OLD.id, OLD.subject, OLD.body);
  INSERT INTO documents_fts (rowid, subject, body)
  VALUES (NEW.id, NEW.subject, NEW.body);
END;
