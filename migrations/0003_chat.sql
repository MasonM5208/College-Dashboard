-- 0003_chat.sql — the chat layer's storage (M5). From SPEC.md §5 and §10.
--
-- SPEC §5 asks for chat_threads and chat_messages, with messages holding "role,
-- content, tool calls, tool results, and token counts". SPEC §10 additionally
-- asks that token counts be logged per message and a running monthly estimate be
-- surfaced, so that spend "never surprises anyone" — which is what the token and
-- model columns below are for.


CREATE TABLE chat_threads (
  id          INTEGER PRIMARY KEY,
  -- Taken from the first question asked, truncated. NULL until that happens.
  title       TEXT,
  created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);


CREATE TABLE chat_messages (
  id                 INTEGER PRIMARY KEY,
  thread_id          INTEGER NOT NULL REFERENCES chat_threads(id) ON DELETE CASCADE,

  role               TEXT NOT NULL CHECK (role IN ('user','assistant','tool')),

  content            TEXT,
  -- A summary of the model's reasoning, when it returns one. Never the raw chain
  -- of thought, which current models do not expose.
  thinking           TEXT,

  -- JSON exactly as it crossed the wire, so a conversation can be replayed and
  -- a misbehaving tool call can be read back verbatim rather than paraphrased.
  tool_calls         TEXT,
  tool_results       TEXT,

  -- Which model produced this turn. Stored per message rather than assumed
  -- globally: switching between Opus and Sonnet changes the price per token, and
  -- a single global constant would silently re-price the whole history.
  model              TEXT,
  stop_reason        TEXT,

  -- Cache reads and writes are counted apart from ordinary input because they
  -- are billed at roughly a tenth and a bit over one times the input rate.
  -- Folding them together would make the running total wrong.
  input_tokens       INTEGER NOT NULL DEFAULT 0,
  output_tokens      INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,

  created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX chat_messages_thread_idx ON chat_messages(thread_id, id);

-- The running monthly estimate reads by month, so it gets its own index.
CREATE INDEX chat_messages_created_idx ON chat_messages(created_at);

CREATE TRIGGER chat_threads_touch_updated_at
AFTER INSERT ON chat_messages FOR EACH ROW
BEGIN
  UPDATE chat_threads
     SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
   WHERE id = NEW.thread_id;
END;
