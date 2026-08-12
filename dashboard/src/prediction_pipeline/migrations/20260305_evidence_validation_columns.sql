-- Add citation validation columns to silver.evidence
-- Non-breaking: all new columns are nullable

BEGIN;

-- Rename quote_text to original_quote for clarity
ALTER TABLE silver.evidence RENAME COLUMN quote_text TO original_quote;

-- Add new columns
ALTER TABLE silver.evidence ADD COLUMN IF NOT EXISTS corrected_quote text;
ALTER TABLE silver.evidence ADD COLUMN IF NOT EXISTS match_type text;
ALTER TABLE silver.evidence ADD COLUMN IF NOT EXISTS verified boolean;
ALTER TABLE silver.evidence ADD COLUMN IF NOT EXISTS chunk_text text;
ALTER TABLE silver.evidence ADD COLUMN IF NOT EXISTS relevance_score float;
ALTER TABLE silver.evidence ADD COLUMN IF NOT EXISTS top_k_levels integer[];

-- Backfill existing rows as unvalidated
UPDATE silver.evidence
SET match_type = 'unvalidated', verified = NULL
WHERE match_type IS NULL;

COMMIT;
