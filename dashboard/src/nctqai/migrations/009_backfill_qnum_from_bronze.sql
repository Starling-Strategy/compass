-- Migration 009: Backfill q_num from bronze.subpolicy_question
--
-- Fixes 12 rows in silver.questions where q_num is wrong or NULL.
-- The correct values already exist in bronze.subpolicy_question (upstream source of truth).
--
-- Issue 2: q_ids 181,182,184,185,186,187 all had q_num='DP9' (generic topic label).
--          Correct values: HSS3, HSS3.1, HSS3.3, HSS3.4, HSS3.5, HSS3.6
--
-- Issue 3: q_ids 55,57,70,92,99,348 had q_num=NULL.
--          Correct values: PT1, PT3, SY7, ASBA4, ASMA4, CB1
--
-- Discovered by auditing Dashboard 23 (PROD Prediction Review Dashboard).
-- See starling.tasks: "Fix Prediction Review Dashboard data quality issues"

BEGIN;

UPDATE silver.questions sq
SET q_num = bsq.q_num
FROM bronze.subpolicy_question bsq
WHERE sq.q_id = bsq.q_id
  AND sq.q_id IN (181, 182, 184, 185, 186, 187,   -- DP9 → HSS3.x
                  55, 57, 70, 92, 99, 348);          -- NULL → PT1, PT3, SY7, ASBA4, ASMA4, CB1

COMMIT;
