-- 0003 — separate the data revision from the report revision.
--
-- Adding a check must not invalidate the comparison datamart.  Before this
-- migration both lived in ``ca.runs.revision``: a CHECKS_ONLY job incremented it,
-- and every query that filtered facts by that revision found nothing, so adding a
-- check silently emptied the pivot table.  Facts now carry their own revision
-- (``data_revision``), while ``revision`` counts published check reports.

ALTER TABLE ca.runs
    ADD COLUMN IF NOT EXISTS data_revision integer NOT NULL DEFAULT 1;

UPDATE ca.runs SET data_revision = 1 WHERE data_revision IS NULL;

--: a check report revision is "published" when none of its executions is still running;
--: this index makes finding the newest published revision cheap.
CREATE INDEX IF NOT EXISTS exec_run_rev_state_idx
    ON ca.check_executions (run_id, revision, state);
