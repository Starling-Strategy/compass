-- Add footnote and page_ref columns to silver.suggested_answers
-- footnote: analyst's footnote text (part of the answer)
-- page_ref: page reference for the answer source
-- Both nullable, no backfill needed.

ALTER TABLE silver.suggested_answers ADD COLUMN IF NOT EXISTS footnote TEXT;
ALTER TABLE silver.suggested_answers ADD COLUMN IF NOT EXISTS page_ref TEXT;
