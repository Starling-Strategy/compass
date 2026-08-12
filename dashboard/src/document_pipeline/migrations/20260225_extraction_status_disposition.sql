-- Migration: 20260225_extraction_status_disposition
-- Purpose: Reclassify failed extraction records with specific disposition codes
-- instead of the generic 'failed' status. This enables better triage and
-- tracking of why documents couldn't be processed.
--
-- New status values:
--   corrupted       - File fetched but PDF is malformed/invalid
--   blocked         - 403 Forbidden — site blocked the crawler
--   dead_link       - 404 Not Found — URL no longer exists
--   timeout         - Connection/read timeout — retriable
--   format_unsupported - File format not handled (non-PDF given to PDF parser, .sql, .txt, etc.)
--   test_record     - Test artifact, not a real document
--
-- 'failed' is retained for any errors that don't match the above patterns.
--
-- Run with: psql "postgresql://postgres:<pw>@<private-db-host>:5432/postgres" -f this_file.sql
-- Dry-run preview: wrap in BEGIN; ... ROLLBACK; instead of COMMIT;

BEGIN;

-- Preview counts before update (comment out in live run)
SELECT
    CASE
        WHEN extraction_error LIKE '%not valid%'                                    THEN 'corrupted'
        WHEN extraction_error LIKE '%File format not allowed%'                      THEN 'format_unsupported'
        WHEN extraction_error LIKE '%403%'                                          THEN 'blocked'
        WHEN extraction_error LIKE '%404%'                                          THEN 'dead_link'
        WHEN extraction_error LIKE '%timed out%'
          OR extraction_error LIKE '%Operation timed out%'
          OR extraction_error LIKE '%The read operation timed out%'                 THEN 'timeout'
        WHEN extraction_error LIKE '%nodename nor servname%'                        THEN 'dead_link'
        WHEN doc_name LIKE '%ABC_Test_District%' OR doc_name LIKE '%_TEST_%'        THEN 'test_record'
        ELSE 'failed'
    END AS new_status,
    COUNT(*) AS count
FROM silver.district_documents
WHERE extraction_status = 'failed'
GROUP BY new_status
ORDER BY count DESC;

-- Apply reclassification
UPDATE silver.district_documents
SET
    extraction_status = CASE
        WHEN extraction_error LIKE '%not valid%'                                    THEN 'corrupted'
        WHEN extraction_error LIKE '%File format not allowed%'                      THEN 'format_unsupported'
        WHEN extraction_error LIKE '%403%'                                          THEN 'blocked'
        WHEN extraction_error LIKE '%404%'                                          THEN 'dead_link'
        WHEN extraction_error LIKE '%timed out%'
          OR extraction_error LIKE '%Operation timed out%'
          OR extraction_error LIKE '%The read operation timed out%'                 THEN 'timeout'
        WHEN extraction_error LIKE '%nodename nor servname%'                        THEN 'dead_link'
        WHEN doc_name LIKE '%ABC_Test_District%' OR doc_name LIKE '%_TEST_%'        THEN 'test_record'
        ELSE 'failed'
    END,
    last_updated = NOW()
WHERE extraction_status = 'failed';

-- Verify results
SELECT extraction_status, COUNT(*) AS count
FROM silver.district_documents
WHERE extraction_status NOT IN ('success', 'pending', 'excluded_navigation', 'ocr_failed')
GROUP BY extraction_status
ORDER BY count DESC;

COMMIT;
