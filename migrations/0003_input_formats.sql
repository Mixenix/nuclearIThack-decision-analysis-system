-- Additive; old registrations and snapshots continue to select JSON.
ALTER TABLE ca.datasets ADD COLUMN IF NOT EXISTS file_glob text NOT NULL DEFAULT '*.json';
