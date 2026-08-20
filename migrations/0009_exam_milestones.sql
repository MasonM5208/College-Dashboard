-- 0009_exam_milestones.sql — study sessions generated from exams (M6).
-- From SPEC.md §9: "An exam 10 days out generates study sessions with estimated
-- hours, which then compete for capacity like any other work."
--
-- Additive only: one nullable column.
--
-- The generated sessions are ordinary rows in `assignments`, deliberately. They
-- are supposed to compete for capacity like anything else, and a separate table
-- would mean teaching the ranking, the overload calculation, the timer and the
-- chat about a second kind of work. One column saying where a row came from is
-- the whole mechanism.

ALTER TABLE assignments ADD COLUMN parent_assignment_id INTEGER
  REFERENCES assignments(id) ON DELETE CASCADE;

-- Finding an exam's sessions, and finding whether they exist at all, are both
-- lookups by parent. Partial, so it indexes only the generated rows rather than
-- every assignment in the table.
CREATE INDEX assignments_parent_idx ON assignments(parent_assignment_id)
  WHERE parent_assignment_id IS NOT NULL;
