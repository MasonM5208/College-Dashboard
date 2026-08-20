-- 0008_capacity.sql — the real capacity model, the timer and calibration (M6).
-- From SPEC.md §5 and §9.
--
-- M2 shipped SPEC §9's "minimum viable" ranking: a flat four productive hours per
-- weekday, no capacity model, no calibration. SPEC's instruction was to ship the
-- constant and "replace it with measured reality in October". These are the
-- tables that replace it.
--
-- THE SEEDED DEFAULTS DELIBERATELY CHANGE NOTHING. capacity_settings starts at
-- 4.0 productive hours and 0 practice hours for all seven days, which is exactly
-- the constant M2 used, so every ranking is identical the day this lands. It
-- changes when Mason edits it and not before. SPEC §9: "Do not attempt to guess
-- the owner's weekly rhythm in August."


-- Fixed weekly obligations. SPEC §5, §9: subtracted from wall-clock time to yield
-- the hours actually available for coursework.
CREATE TABLE commitments (
  id          INTEGER PRIMARY KEY,
  term_id     INTEGER NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
  label       TEXT NOT NULL,                 -- 'Wind Ensemble', 'MATH 211 lecture'

  kind        TEXT NOT NULL DEFAULT 'other'
              CHECK (kind IN ('class','ensemble','lesson','practice','work','other')),

  -- 0 = Monday, matching Python's date.weekday(). Chosen over Sunday-first
  -- because every calculation in app/ is Python and one convention is worth more
  -- than matching any particular calendar's column order.
  weekday     INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),

  -- Local wall-clock, 'HH:MM'. Not UTC: a rehearsal is at seven in the evening
  -- regardless of what the clocks did in March, and storing it in UTC would move
  -- it by an hour twice a year.
  start_time  TEXT NOT NULL,
  end_time    TEXT NOT NULL,

  course_id   INTEGER REFERENCES courses(id) ON DELETE SET NULL,
  active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
  created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),

  CHECK (end_time > start_time)
);

CREATE INDEX commitments_weekday_idx ON commitments(weekday, active);


-- The per-weekday budget. SPEC §5.
--
-- practice_hours_target is the load-bearing column and the reason SPEC argues the
-- point at length: "Practice is modeled as capacity consumption, not as a task.
-- This is a design decision, not a preference. Practice has no due date, so in any
-- deadline-driven ranking it silently loses every comparison — and the owner is a
-- performance major who will not notice the degradation until roughly a month in."
--
-- Subtracting it here means the priority maths protects practice by default,
-- without practice ever having to win an argument against a deadline.
CREATE TABLE capacity_settings (
  id                    INTEGER PRIMARY KEY,
  weekday               INTEGER NOT NULL UNIQUE CHECK (weekday BETWEEN 0 AND 6),
  productive_hours      REAL NOT NULL DEFAULT 4.0 CHECK (productive_hours >= 0),
  practice_hours_target REAL NOT NULL DEFAULT 0.0 CHECK (practice_hours_target >= 0)
);

-- 0 = Monday. Four hours a day is M2's constant, so nothing re-ranks on arrival.
INSERT INTO capacity_settings (weekday, productive_hours, practice_hours_target)
VALUES (0, 4.0, 0.0), (1, 4.0, 0.0), (2, 4.0, 0.0), (3, 4.0, 0.0),
       (4, 4.0, 0.0), (5, 4.0, 0.0), (6, 4.0, 0.0);


-- Time actually spent, from the start/stop timer. SPEC §5, §9.
CREATE TABLE time_entries (
  id            INTEGER PRIMARY KEY,
  assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
  started_at    TEXT NOT NULL,
  -- NULL while the timer is running. At most one row may be in that state, which
  -- the partial unique index below enforces rather than the application hoping.
  ended_at      TEXT,
  minutes       REAL,
  note          TEXT
);

CREATE INDEX time_entries_assignment_idx ON time_entries(assignment_id, id);

-- One running timer at a time, enforced by the database. Two open timers would
-- make every calibration figure quietly wrong, and the bug would be invisible
-- until the numbers had been trusted for a month.
CREATE UNIQUE INDEX time_entries_one_running_idx
  ON time_entries((ended_at IS NULL)) WHERE ended_at IS NULL;


-- The per-type multiplier, recomputed as entries complete. SPEC §5, §9.
--
-- SPEC expects papers to be underestimated by roughly 2x at first: "Everyone
-- does." The multiplier is stored rather than computed on the fly so that the
-- number shown on screen and the number used to seed a default are provably the
-- same one, and so that updated_at can say how current it is.
CREATE TABLE estimate_calibration (
  assignment_type TEXT PRIMARY KEY
                  CHECK (assignment_type IN ('worksheet','paper','project','exam',
                                             'quiz','performance','milestone','other')),
  sample_count    INTEGER NOT NULL DEFAULT 0,
  multiplier      REAL NOT NULL DEFAULT 1.0 CHECK (multiplier > 0),
  updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
