-- 0004 — pin each dataset to the DATASET_ROOT it was registered under.
--
-- ``root_ref`` alone is only meaningful relative to the root that was configured
-- at registration time.  When DATASET_ROOT changed afterwards, every registration
-- silently pointed at the wrong directory and the only symptom was a confusing
-- "manifest.json not found".  The root is now recorded with the dataset, so a
-- later configuration change cannot reinterpret existing registrations.

ALTER TABLE ca.datasets ADD COLUMN IF NOT EXISTS abs_root text;

-- existing rows are left NULL on purpose: they fall back to the configured
-- DATASET_ROOT, which is the behaviour they had when they were created.
