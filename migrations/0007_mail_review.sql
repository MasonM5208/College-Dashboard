-- 0007_mail_review.sql — the holding area for automatically collected mail (M4).
--
-- SPEC §7 lists an automatic email path but assumed it would be a Gmail POP pull.
-- The institution blocks forwarding out and Gmail offers no pull, so the route
-- that actually works is: Outlook auto-forwards to a mailbox used for nothing
-- else, and the dashboard reads that mailbox over IMAP.
--
-- WHY A QUEUE RATHER THAN STRAIGHT INTO documents
--
-- SPEC §7's argument for keyword search over vectors rests on the archive being
-- "curated rather than exhaustive — roughly the few dozen messages per semester
-- that actually matter, not thousands including every listserv blast". Piping a
-- whole university mail account into `documents` would take that premise away:
-- searches would start returning dining-hall menus, and the archive would stop
-- being a thing every entry of which is known to matter.
--
-- So collected mail waits here until it is kept or discarded. This is the same
-- shape as M1's review queue for Canvas events whose course cannot be identified
-- (SPEC §6.4): nothing is thrown away, and nothing is admitted on the machine's
-- say-so.


CREATE TABLE inbound_messages (
  id           INTEGER PRIMARY KEY,

  -- IMAP's UID, prefixed with the folder's UIDVALIDITY. A mailbox that is
  -- rebuilt reissues UIDs from 1, and UIDVALIDITY is the server telling us that
  -- happened — without it in the key, message 1 of the new mailbox would look
  -- like message 1 of the old one and be skipped forever.
  uid          TEXT NOT NULL,

  -- RFC 5322 Message-ID, kept as the provenance identifier so the same message
  -- collected again after a mailbox rebuild is recognisable.
  message_id   TEXT,

  -- Same normalised hash the archive uses, computed at collection time so an
  -- already-saved message never appears in the queue at all.
  body_sha256  TEXT NOT NULL,

  subject      TEXT,
  sender       TEXT,
  received_at  TEXT,
  body         TEXT NOT NULL,
  raw_headers  TEXT,

  state        TEXT NOT NULL DEFAULT 'pending'
               CHECK (state IN ('pending','kept','discarded')),

  -- Set when kept. ON DELETE SET NULL rather than CASCADE: deleting a document
  -- from the archive should not erase the record that it was once collected and
  -- kept, or the same message would silently reappear in the queue next poll.
  document_id  INTEGER REFERENCES documents(id) ON DELETE SET NULL,

  fetched_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  decided_at   TEXT
);

-- Discarded rows are kept, not deleted, which is what stops a message you have
-- already said no to coming back on the next poll.
CREATE UNIQUE INDEX inbound_messages_uid_idx ON inbound_messages(uid);
CREATE INDEX inbound_messages_state_idx ON inbound_messages(state, id);
CREATE INDEX inbound_messages_hash_idx ON inbound_messages(body_sha256);
