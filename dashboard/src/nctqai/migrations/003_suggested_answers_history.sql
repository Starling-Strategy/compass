-- 003_suggested_answers_history.sql
-- Add dashboard view for latest-row-wins display on suggested_answers.
--
-- Backward-compatible: keeps original PK (district_id, ay_id, q_id) so
-- Nathan's ON CONFLICT upsert code still works. Our branch does plain
-- INSERT (always sets run_id), so history accumulates from our runs.
-- Nathan's ON CONFLICT overwrites the single row per question as before.
--
-- When Nathan's branch adopts plain INSERT too, a follow-up migration
-- can change the PK to (district_id, ay_id, q_id, run_id).

BEGIN;

-- 1. Index for efficient latest-per-question lookup (used by the view)
CREATE INDEX IF NOT EXISTS idx_suggested_answers_latest
  ON silver.suggested_answers (district_id, ay_id, q_id, created_at DESC);

-- 2. View: latest row per (district, ay, question) — all dashboard reads use this
CREATE OR REPLACE VIEW silver.v_latest_suggested_answers AS
SELECT DISTINCT ON (district_id, ay_id, q_id) *
FROM silver.suggested_answers
WHERE source = 'piedpiper'
ORDER BY district_id, ay_id, q_id, created_at DESC;

COMMIT;
