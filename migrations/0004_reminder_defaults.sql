-- 0004_reminder_defaults.sql — SPEC §8's default reminder ladders (M3).
--
-- No table changes: reminder_rules and reminder_instances have existed since
-- 0001. This seeds the ladders as data rather than leaving them in code, which is
-- what SPEC §8 means by "tunable per course and per type once the owner knows his
-- professors" — editing them later becomes a form, not a deploy.
--
-- Offsets are ISO 8601 durations measured back from the due time. The scope is
-- 'assignment_type', which the CHECK constraint on reminder_rules already
-- requires to come with an assignment_type and no course.
--
-- SPEC §8 gives no ladder for 'other'. It gets the worksheet one — two nudges,
-- the day before and a few hours out — because silence is the worse default.

INSERT INTO reminder_rules (scope, assignment_type, offsets_json, enabled) VALUES
  -- Small, self-contained work: SPEC §8 gives 24h and 3h.
  ('assignment_type', 'worksheet',   '["P1D","PT3H"]', 1),
  ('assignment_type', 'quiz',        '["P1D","PT3H"]', 1),
  ('assignment_type', 'other',       '["P1D","PT3H"]', 1),

  -- Long work. The start_by rung is not an offset — it comes from
  -- assignments.start_by, which entry.py computes as due minus est_hours x 2 days
  -- per SPEC §5. It is the rung that stops a paper losing to a worksheet.
  ('assignment_type', 'paper',       '["P7D","P3D","P1D","MORNING_OF"]', 1),
  ('assignment_type', 'project',     '["P7D","P3D","P1D","MORNING_OF"]', 1),

  -- Exams need the long runway: SPEC §8 gives 10d, 5d, 2d and the night before.
  ('assignment_type', 'exam',        '["P10D","P5D","P2D","NIGHT_BEFORE"]', 1),

  -- Performances and milestones: a weekly checkpoint from four weeks out.
  ('assignment_type', 'performance', '["P28D","P21D","P14D","P7D"]', 1),
  ('assignment_type', 'milestone',   '["P28D","P21D","P14D","P7D"]', 1);
