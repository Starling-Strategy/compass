-- Migration 008: Convert rejection_reason from single string to JSON array
-- Supports multi-select rejection reasons in the review UI.
-- Column type stays TEXT; value changes from "wrong_doc_cited" to '["wrong_doc_cited"]'.

BEGIN;

UPDATE silver.suggested_answers
SET rejection_reason = '["' || rejection_reason || '"]'
WHERE rejection_reason IS NOT NULL
  AND rejection_reason NOT LIKE '[%';

UPDATE silver.review_audit_log
SET rejection_reason = '["' || rejection_reason || '"]'
WHERE rejection_reason IS NOT NULL
  AND rejection_reason NOT LIKE '[%';

COMMIT;
