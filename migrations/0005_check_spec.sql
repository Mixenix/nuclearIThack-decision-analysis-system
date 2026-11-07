-- 0005 — keep the executed check specification with each published revision.
--
-- A later CHECKS_ONLY job must republish the *whole* configured set, so it needs
-- the code of the checks that ran before.  Deriving that from ``runs.snapshot``
-- alone is wrong: the snapshot is only authoritative for runs created through
-- the normal pipeline, and it can be stale.  The published revision is the
-- authority, so its specification is stored next to the execution.

ALTER TABLE ca.check_executions ADD COLUMN IF NOT EXISTS spec jsonb;
