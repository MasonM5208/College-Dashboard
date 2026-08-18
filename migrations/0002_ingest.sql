-- 0002_ingest.sql — supports Canvas ICS ingestion (M1). See SPEC.md §6.
--
-- Both statements are additive. No column is dropped, retyped or rebuilt, so an
-- existing database keeps everything it already holds.
--
-- WHY THESE TWO COLUMNS EXIST
--
-- The feed identifies a course by an SIS code in the bracketed suffix of an event
-- title, for example [FA26-BL-MATH-M211-2050]. That code is a good matching key
-- and a poor label, and the feed carries no readable course name anywhere.
--
-- So the first time a code is seen, ingestion creates the course itself rather than
-- parking its assignments in a queue until Mason gets round to it. The code stands
-- in as the name, and needs_naming marks the row as awaiting a real one.
--
-- Creating a course means creating a term, because courses.term_id is NOT NULL and
-- terms requires start_date and end_date. The code prefix gives the term its name
-- (FA26), and the dates are seeded from the range of events in the feed — a guess,
-- and flagged as one by needs_dates, because SPEC §9's display rules are built on
-- never presenting a guess as though it were established fact.


ALTER TABLE courses ADD COLUMN needs_naming INTEGER NOT NULL DEFAULT 0
  CHECK (needs_naming IN (0,1));

ALTER TABLE terms ADD COLUMN needs_dates INTEGER NOT NULL DEFAULT 0
  CHECK (needs_dates IN (0,1));
