-- 0005_chat_threads.sql — naming and keeping conversations.
--
-- Additive only: one column with a default, so applying this to a database that
-- already holds conversations cannot lose any of them.
--
-- Why a "kept" flag rather than a folder or a tag: conversations here fall into
-- two groups only — the throwaway question asked once, and the handful worth
-- coming back to. A flag sorts the second group to the top of the list and needs
-- no vocabulary to maintain. Anything richer is a filing system, and a filing
-- system that has to be tended is one more thing to abandon in November.

ALTER TABLE chat_threads ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;

-- The list orders by kept-first, then most recent. Small table, but the index
-- costs nothing and the ordering is on every page load of the chat.
CREATE INDEX chat_threads_order_idx ON chat_threads(pinned DESC, updated_at DESC);
