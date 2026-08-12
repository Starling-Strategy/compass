-- Migration: Normalize src_type and ai_document_type to unified NctqDocumentType enum
-- Date: 2026-02-23
-- Target: Production Azure PostgreSQL
-- Purpose: Clean up mixed src_type values → 8 clean snake_case NCTQ categories
--
-- The 8 categories: salary_schedule, annual_calendar, evaluation_handbook,
--   contract, union_document, benefits_handbook, board_policy, other

BEGIN;

-- 1. Backup current values (idempotent — won't overwrite existing backups)
ALTER TABLE silver.district_documents ADD COLUMN IF NOT EXISTS src_type_backup TEXT;
ALTER TABLE silver.district_documents ADD COLUMN IF NOT EXISTS ai_document_type_backup TEXT;

UPDATE silver.district_documents SET src_type_backup = src_type WHERE src_type_backup IS NULL;
UPDATE silver.district_documents SET ai_document_type_backup = ai_document_type WHERE ai_document_type_backup IS NULL;

-- 2. Normalize src_type (human classification from bronze)
--    Bronze uses Title Case ("Salary Schedule"), old pipeline stored file formats ("pdf")
UPDATE silver.district_documents SET src_type = CASE LOWER(TRIM(src_type))
    WHEN 'salary schedule' THEN 'salary_schedule'
    WHEN 'annual calendar' THEN 'annual_calendar'
    WHEN 'evaluation handbook' THEN 'evaluation_handbook'
    WHEN 'contract' THEN 'contract'
    WHEN 'union document' THEN 'union_document'
    WHEN 'benefits handbook' THEN 'benefits_handbook'
    WHEN 'board policy' THEN 'board_policy'
    WHEN 'other' THEN 'other'
    -- File format values stored by old normalize_src_type()
    WHEN 'pdf' THEN 'other'
    WHEN 'docx' THEN 'other'
    WHEN 'doc' THEN 'other'
    WHEN 'xlsx' THEN 'other'
    WHEN 'web_page' THEN 'other'
    WHEN 'scraped_page' THEN 'other'
    ELSE 'other'
END;

-- 3. Normalize ai_document_type (AI classification → same 8 categories)
--    Old AI enum had: policy, budget, report, handbook, calendar — map to NCTQ equivalents
UPDATE silver.district_documents SET ai_document_type = CASE LOWER(TRIM(ai_document_type))
    WHEN 'contract' THEN 'contract'
    WHEN 'salary_schedule' THEN 'salary_schedule'
    WHEN 'evaluation_handbook' THEN 'evaluation_handbook'
    WHEN 'policy' THEN 'board_policy'
    WHEN 'board_policy' THEN 'board_policy'
    WHEN 'calendar' THEN 'annual_calendar'
    WHEN 'annual_calendar' THEN 'annual_calendar'
    WHEN 'union_document' THEN 'union_document'
    WHEN 'benefits_handbook' THEN 'benefits_handbook'
    WHEN 'budget' THEN 'other'
    WHEN 'report' THEN 'other'
    WHEN 'handbook' THEN 'other'
    WHEN 'other' THEN 'other'
    ELSE ai_document_type  -- leave NULL as NULL, keep unknowns
END
WHERE ai_document_type IS NOT NULL;

COMMIT;

-- Verification queries (run after migration):
-- SELECT src_type, COUNT(*) FROM silver.district_documents GROUP BY 1 ORDER BY 2 DESC;
-- SELECT ai_document_type, COUNT(*) FROM silver.district_documents WHERE ai_document_type IS NOT NULL GROUP BY 1 ORDER BY 2 DESC;
