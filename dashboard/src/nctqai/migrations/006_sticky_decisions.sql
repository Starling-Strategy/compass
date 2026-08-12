-- 006_sticky_decisions.sql
-- Reviewed answers become "sticky" — the view prioritizes rows where an analyst
-- has made a decision (approved/incorrect) over newer unreviewed rows.
-- "Reset to Unreviewed" releases the sticky hold, surfacing the latest row.

BEGIN;

-- 1. Expression index for the new view ordering
CREATE INDEX IF NOT EXISTS idx_suggested_answers_sticky
  ON silver.suggested_answers (
    district_id, ay_id, q_id,
    (CASE WHEN status IN ('approved', 'incorrect') THEN 0 ELSE 1 END),
    created_at DESC
  )
  WHERE source = 'piedpiper';

-- 2. Replace view with sticky-decision ordering
CREATE OR REPLACE VIEW silver.v_latest_suggested_answers AS
SELECT DISTINCT ON (district_id, ay_id, q_id) *
FROM silver.suggested_answers
WHERE source = 'piedpiper'
ORDER BY district_id, ay_id, q_id,
    CASE WHEN status IN ('approved', 'incorrect') THEN 0 ELSE 1 END,
    created_at DESC;

COMMIT;
