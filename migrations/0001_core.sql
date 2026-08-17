-- 0001_core.sql — the tables M1 through M3 are built on. From SPEC.md §5.
--
-- RULES FOR EVERY FILE IN THIS DIRECTORY
--
--   1. Once a migration has been applied it is frozen. Editing it is detected and
--      refused at startup (app/migrate.py checksums applied files). Corrections go
--      in a new numbered file.
--   2. No BEGIN, COMMIT, ROLLBACK or VACUUM. The runner supplies the transaction.
--   3. Anything that drops or alters an existing column is a destructive change:
--      ask Mason before writing it (CLAUDE.md).
--
-- CONVENTIONS
--
--   * Timestamps are TEXT in ISO 8601 UTC, 'YYYY-MM-DDTHH:MM:SSZ'. That shape
--     sorts chronologically as text, so ORDER BY works with no conversion. Local
--     time exists only for display.
--   * Dates without a time are TEXT 'YYYY-MM-DD'.
--   * Booleans are INTEGER 0 or 1, with a CHECK constraint. SQLite has no boolean.
--   * SQLite has no enum type either, so the enums in SPEC §5 are CHECK
--     constraints. Adding a value later means a new table, so the lists here are
--     copied from the spec exactly.
--   * Deliberately NOT deferred to later milestones: sync_state and audit_log.
--     SPEC §5 calls both "painful to retrofit".


-- Academic terms. One row per semester.
CREATE TABLE terms (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,          -- 'Fall 2026'
  start_date  TEXT NOT NULL,
  end_date    TEXT NOT NULL,
  CHECK (end_date >= start_date)
);


-- Courses within a term.
CREATE TABLE courses (
  id                   INTEGER PRIMARY KEY,
  term_id              INTEGER NOT NULL REFERENCES terms(id) ON DELETE RESTRICT,
  name                 TEXT NOT NULL,
  code                 TEXT,                 -- 'MUS-P 350'
  instructor           TEXT,
  meeting_pattern      TEXT,                 -- free text, e.g. 'MWF 10:10-11:00'

  -- SPEC §6.4: the Canvas feed has no course field. Association comes from the
  -- bracketed suffix of an event's SUMMARY, matched against this string. NULL
  -- until the first matching event is confirmed in the review queue.
  ics_summary_pattern  TEXT,

  credits              REAL,
  notes                TEXT,

  -- SPEC §5/§9: what is rational to sacrifice under overload. Free text for the
  -- syllabus wording, plus the structured number where it is known.
  late_policy          TEXT,
  penalty_pct_per_day  REAL,

  -- SPEC §5: owner-maintained, nullable. Discounts marginal grade impact — a 25%
  -- paper matters more at 79% than at 96%.
  current_grade_pct    REAL,

  CHECK (credits IS NULL OR credits >= 0),
  CHECK (penalty_pct_per_day IS NULL OR penalty_pct_per_day >= 0),
  CHECK (current_grade_pct IS NULL OR (current_grade_pct >= 0 AND current_grade_pct <= 100))
);

CREATE INDEX courses_term_idx ON courses(term_id);

-- One course may only claim a given ICS pattern once. SQLite treats NULLs as
-- distinct in a UNIQUE index, so unmatched courses do not collide with each other.
CREATE UNIQUE INDEX courses_ics_pattern_idx ON courses(ics_summary_pattern);


-- Assignments, exams, performances — anything with work attached.
CREATE TABLE assignments (
  id                    INTEGER PRIMARY KEY,

  -- Nullable on purpose. SPEC §6.5 requires that a feed event whose course cannot
  -- be identified go to a review queue rather than being dropped, and SPEC §9's
  -- quick capture accepts anything before it has been triaged. How the review
  -- queue is presented is an M1 decision; this column is what makes it possible.
  course_id             INTEGER REFERENCES courses(id) ON DELETE RESTRICT,

  title                 TEXT NOT NULL,

  type                  TEXT NOT NULL DEFAULT 'other'
                          CHECK (type IN ('worksheet','paper','project','exam',
                                          'quiz','performance','milestone','other')),

  -- Nullable: quick-capture items have no date yet, and SPEC §6.3 notes that
  -- anything a professor assigns without a Canvas due date is invisible to the
  -- feed and gets entered by hand.
  due_at                TEXT,

  -- SPEC §5: defaults to due_at - (est_hours x 2 days) for papers and projects,
  -- computed on write, always overridable.
  start_by              TEXT,

  -- SPEC §9: "The prioritization engine is inert without this field populated."
  est_hours             REAL,
  est_hours_remaining   REAL,

  points_possible       REAL,
  weight_category       TEXT,                -- 'Exams', 'Homework' — free text in M2

  status                TEXT NOT NULL DEFAULT 'not_started'
                          CHECK (status IN ('not_started','in_progress','submitted',
                                            'graded','dismissed')),

  source                TEXT NOT NULL
                          CHECK (source IN ('ics','manual','syllabus_batch')),

  -- SPEC §6.1: stable across polls, so this is the join key for feed diffing.
  -- UNIQUE permits many NULLs in SQLite, which is what manual entries need.
  ics_uid               TEXT UNIQUE,

  -- SPEC §6.6: an event vanishing from the feed usually means it was deleted or
  -- unpublished, but it can also be a transient feed error, and "a transient feed
  -- error must never destroy data". So nothing is hard-deleted: the timestamp of
  -- the first poll that failed to see this item is recorded here and surfaced for
  -- confirmation. Cleared if the item reappears.
  feed_missing_since    TEXT,

  late_penalty_override REAL,

  -- SPEC §5: forces an item to the top of the Today view regardless of slack.
  pinned                INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0,1)),

  created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),

  CHECK (est_hours IS NULL OR est_hours >= 0),
  CHECK (est_hours_remaining IS NULL OR est_hours_remaining >= 0)
);

CREATE INDEX assignments_due_idx ON assignments(due_at);
CREATE INDEX assignments_course_idx ON assignments(course_id);
CREATE INDEX assignments_status_idx ON assignments(status);

-- The review queue of items the feed stopped mentioning (SPEC §6.6). Partial, so
-- it stays tiny: almost every row has feed_missing_since NULL and is not indexed
-- here. The other review queue — feed items with no course yet — is served by
-- assignments_course_idx above, which indexes NULLs like any other value.
CREATE INDEX assignments_feed_missing_idx ON assignments(feed_missing_since)
  WHERE feed_missing_since IS NOT NULL;

-- Keeps updated_at honest without every caller having to remember it. The WHEN
-- clause lets a caller set updated_at explicitly and prevents the trigger from
-- recursing into itself.
CREATE TRIGGER assignments_touch_updated_at
AFTER UPDATE ON assignments FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE assignments
     SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
   WHERE id = NEW.id;
END;


-- Which offsets to remind at. SPEC §8 gives the defaults; these are tunable per
-- course and per assignment type once Mason knows his professors.
CREATE TABLE reminder_rules (
  id              INTEGER PRIMARY KEY,

  scope           TEXT NOT NULL
                    CHECK (scope IN ('global','course','assignment_type')),

  course_id       INTEGER REFERENCES courses(id) ON DELETE CASCADE,
  assignment_type TEXT CHECK (assignment_type IS NULL OR assignment_type IN
                    ('worksheet','paper','project','exam','quiz','performance',
                     'milestone','other')),

  -- JSON array of offsets before the target time, as ISO 8601 durations:
  -- ["P7D","P3D","P1D","PT3H"]. Stored as text because these are read as a unit
  -- and never queried across.
  offsets_json    TEXT NOT NULL,

  enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),

  -- The scope column decides which of the two targeting columns must be present.
  CHECK (
    (scope = 'global'          AND course_id IS NULL AND assignment_type IS NULL) OR
    (scope = 'course'          AND course_id IS NOT NULL) OR
    (scope = 'assignment_type' AND assignment_type IS NOT NULL)
  )
);

CREATE INDEX reminder_rules_course_idx ON reminder_rules(course_id);


-- One row per individual reminder. SPEC §8: "Materialize every reminder as its own
-- row. This is what makes individual snoozing possible and keeps state coherent
-- when a due date moves."
--
-- When a due date changes, affected pending rows are marked 'superseded' and new
-- rows are generated. fire_at is never mutated in place, so the history of what
-- was scheduled and what actually went out stays auditable.
CREATE TABLE reminder_instances (
  id            INTEGER PRIMARY KEY,

  assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,

  -- Nullable: a reminder created ad hoc has no rule behind it. SET NULL rather
  -- than CASCADE, because deleting a rule must not erase the record of reminders
  -- that already fired.
  rule_id       INTEGER REFERENCES reminder_rules(id) ON DELETE SET NULL,

  kind          TEXT NOT NULL CHECK (kind IN ('start_by','due_by')),

  -- Already adjusted for quiet hours 22:30-07:30 (SPEC §8): due_by reminders move
  -- earlier, start_by reminders move later.
  fire_at       TEXT NOT NULL,

  -- SPEC §8 splits the channels and forbids overlap: CalDAV carries every
  -- time-based nag, web push carries event-driven notifications only.
  channel       TEXT NOT NULL CHECK (channel IN ('caldav','web_push')),

  state         TEXT NOT NULL DEFAULT 'pending'
                  CHECK (state IN ('pending','sent','snoozed','dismissed','superseded')),

  sent_at       TEXT,

  -- Identifier returned by the delivery channel: the CalDAV item's UID, so a
  -- superseded reminder can be withdrawn from Apple Reminders.
  external_id   TEXT
);

-- The reminder sweep's query: pending rows that are now due to fire.
CREATE INDEX reminder_instances_due_idx ON reminder_instances(state, fire_at);
CREATE INDEX reminder_instances_assignment_idx ON reminder_instances(assignment_id);


-- One row per scheduled job. SPEC §4: "Every scheduled job writes to sync_state
-- and logs its outcome." SPEC §6: three consecutive failures must produce a
-- prominent warning, and a failed poll must never look like "nothing due".
CREATE TABLE sync_state (
  source               TEXT PRIMARY KEY,     -- 'canvas_ics', 'caldav_push', 'backup'
  last_success_at      TEXT,
  last_attempt_at      TEXT,
  last_error           TEXT,                 -- never a secret; see SPEC §11
  cursor               TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0
);


-- Append-only history of every write to assignments, documents and
-- reminder_instances (SPEC §5).
--
-- Named table_name rather than SPEC's `table` because `table` is an SQL keyword
-- and would need quoting at every use site.
CREATE TABLE audit_log (
  id          INTEGER PRIMARY KEY,
  timestamp   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  action      TEXT NOT NULL,                 -- 'create', 'update', 'status_change'
  table_name  TEXT NOT NULL,
  record_id   INTEGER,
  detail_json TEXT
);

CREATE INDEX audit_log_record_idx ON audit_log(table_name, record_id);
CREATE INDEX audit_log_timestamp_idx ON audit_log(timestamp);

-- "Append-only" enforced by the database, not by good intentions.
CREATE TRIGGER audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
  SELECT RAISE(ABORT, 'audit_log is append-only: rows cannot be changed');
END;

CREATE TRIGGER audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
  SELECT RAISE(ABORT, 'audit_log is append-only: rows cannot be deleted');
END;
