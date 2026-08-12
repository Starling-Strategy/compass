BEGIN;

ALTER TABLE silver.districts
  ADD COLUMN is_in_universe boolean NOT NULL DEFAULT false;

UPDATE silver.districts sd
SET is_in_universe = true
FROM bronze.district bd
WHERE bd.district_id = sd.district_id
  AND bd.district_deleted_flag = false
  AND bd.district_inactive_flag = false;

COMMIT;

-- Verify: should return 148
SELECT COUNT(*) FROM silver.districts WHERE is_in_universe = true;
