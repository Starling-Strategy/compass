-- 007_fix_markdown_garbage.sql
-- Fix 11 nkotb-v3 rows where suggested_answer contains markdown garbage.
-- These rows had synthesis vote for a garbage string instead of INA.

BEGIN;

UPDATE silver.suggested_answers
SET suggested_answer = 'INA',
    is_ina = TRUE,
    status = 'unreviewed'
WHERE model_version = 'nkotb-v3'
  AND (suggested_answer LIKE '## %'
       OR suggested_answer LIKE '**%'
       OR suggested_answer LIKE '#%');

COMMIT;
