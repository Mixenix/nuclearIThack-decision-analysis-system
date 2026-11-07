-- 0002 — what the runtime needs beyond the first sketch.
--
-- 1. ``fact_index``: the aggregate stage produces facts in a deterministic order
--    and writes them with COPY, which cannot return generated ids.  Keeping the
--    producer's own index lets the membership rows be joined back without a
--    second round trip per fact.
-- 2. A read-only role for user-supplied check code.  A check never needs to
--    write; running it under a role that *cannot* write is cheaper than trusting
--    it (PLAN.md §11.6).

ALTER TABLE ca.pivot_facts ADD COLUMN IF NOT EXISTS fact_index integer NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX IF NOT EXISTS facts_index_uidx
    ON ca.pivot_facts (run_id, revision, fact_index);

CREATE INDEX IF NOT EXISTS cmp_adjusted_idx
    ON ca.comparison_rows (run_id, adjusted_before);
CREATE INDEX IF NOT EXISTS cmp_value_after_idx
    ON ca.comparison_rows (run_id, value_after);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ca_ro') THEN
        CREATE ROLE ca_ro LOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA ca TO ca_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA ca TO ca_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA ca GRANT SELECT ON TABLES TO ca_ro;
