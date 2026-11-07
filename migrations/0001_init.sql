-- 0001_init.sql — initial schema.
--
-- Conventions (PLAN.md §13.1):
--   * internal ids are uuid, APP_ID stays text (never assume a numeric format);
--   * timestamps are timestamptz, stored in UTC;
--   * flexible/extensible payloads are jsonb, hot filter columns are real columns;
--   * one physical table set for all users, scoped by owner_id / run_id.
--
-- This migration is idempotent-safe when wrapped in a transaction by the
-- migration runner and must never drop user data on normal startup.

CREATE SCHEMA IF NOT EXISTS ca;

-- ---------------------------------------------------------------------------
-- accounts and sessions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ca.users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    login         text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    display_name  text NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ca.sessions (
    id         text PRIMARY KEY,               -- opaque random token
    user_id    uuid NOT NULL REFERENCES ca.users(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user_idx ON ca.sessions (user_id);
CREATE INDEX IF NOT EXISTS sessions_exp_idx  ON ca.sessions (expires_at);

-- ---------------------------------------------------------------------------
-- datasets = a registered pair of slices on disk
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ca.datasets (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id   uuid REFERENCES ca.users(id) ON DELETE CASCADE,
    name       text NOT NULL,
    root_ref   text NOT NULL,                  -- path relative to DATASET_ROOT
    manifest   jsonb NOT NULL DEFAULT '{}'::jsonb,
    shared     boolean NOT NULL DEFAULT true,
    app_pairs  integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (root_ref)
);

-- ---------------------------------------------------------------------------
-- user procedures: two sources (workspace text | registered .py file)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ca.procedures (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id    uuid NOT NULL REFERENCES ca.users(id) ON DELETE CASCADE,
    stage       text NOT NULL,                 -- normalize | compare | aggregate | check
    name        text NOT NULL,
    source_kind text NOT NULL,                 -- workspace | file
    code        text,                          -- workspace source text
    path_ref    text,                          -- SCRIPT_ROOT-relative .py path
    params      jsonb NOT NULL DEFAULT '{}'::jsonb,
    builtin     boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS procedures_owner_idx ON ca.procedures (owner_id, stage);

-- ---------------------------------------------------------------------------
-- configurations and runs
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ca.configurations (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id   uuid NOT NULL REFERENCES ca.users(id) ON DELETE CASCADE,
    dataset_id uuid REFERENCES ca.datasets(id) ON DELETE SET NULL,
    name       text NOT NULL,
    selections jsonb NOT NULL DEFAULT '{}'::jsonb,
    params     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS configurations_owner_idx ON ca.configurations (owner_id);

CREATE TABLE IF NOT EXISTS ca.runs (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id      uuid NOT NULL REFERENCES ca.users(id) ON DELETE CASCADE,
    dataset_id    uuid REFERENCES ca.datasets(id) ON DELETE SET NULL,
    config_id     uuid REFERENCES ca.configurations(id) ON DELETE SET NULL,
    name          text NOT NULL DEFAULT '',
    snapshot      jsonb NOT NULL DEFAULT '{}'::jsonb,  -- immutable run parameters
    state         text NOT NULL DEFAULT 'QUEUED',
    backend       text NOT NULL DEFAULT 'cpu',
    expected_pairs integer NOT NULL DEFAULT 0,
    processed_pairs integer NOT NULL DEFAULT 0,
    summary       jsonb NOT NULL DEFAULT '{}'::jsonb,
    revision      integer NOT NULL DEFAULT 1,          -- published report revision
    error_code    text,
    error_message text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    started_at    timestamptz,
    finished_at   timestamptz
);
CREATE INDEX IF NOT EXISTS runs_owner_idx ON ca.runs (owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS runs_state_idx ON ca.runs (state);

-- ---------------------------------------------------------------------------
-- queue
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ca.jobs (
    id              bigserial PRIMARY KEY,
    owner_id        uuid NOT NULL REFERENCES ca.users(id) ON DELETE CASCADE,
    run_id          uuid NOT NULL REFERENCES ca.runs(id) ON DELETE CASCADE,
    kind            text NOT NULL,             -- PIPELINE | CHECKS_ONLY | LLM
    state           text NOT NULL DEFAULT 'QUEUED',
    worker_id       text,
    lease_until     timestamptz,
    attempt         integer NOT NULL DEFAULT 0,
    current_stage   text,
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key text,
    error_code      text,
    error_message   text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    heartbeat_at    timestamptz,
    finished_at     timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_idem_idx ON ca.jobs (idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS jobs_queue_idx ON ca.jobs (state, created_at);
CREATE INDEX IF NOT EXISTS jobs_run_idx   ON ca.jobs (run_id);

CREATE TABLE IF NOT EXISTS ca.run_steps (
    id             bigserial PRIMARY KEY,
    run_id         uuid NOT NULL REFERENCES ca.runs(id) ON DELETE CASCADE,
    job_id         bigint REFERENCES ca.jobs(id) ON DELETE CASCADE,
    stage          text NOT NULL,
    seq            integer NOT NULL DEFAULT 0,
    state          text NOT NULL DEFAULT 'PENDING',
    progress_done  integer NOT NULL DEFAULT 0,
    progress_total integer NOT NULL DEFAULT 0,
    procedure_kind text NOT NULL DEFAULT 'workspace',
    procedure_ref  text NOT NULL DEFAULT '',   -- procedure id or file path
    procedure_hash text NOT NULL DEFAULT '',
    detail         jsonb NOT NULL DEFAULT '{}'::jsonb,
    error          text,
    started_at     timestamptz,
    finished_at    timestamptz
);
CREATE INDEX IF NOT EXISTS run_steps_run_idx ON ca.run_steps (run_id, id);

CREATE TABLE IF NOT EXISTS ca.run_events (
    id         bigserial PRIMARY KEY,
    run_id     uuid NOT NULL REFERENCES ca.runs(id) ON DELETE CASCADE,
    step_id    bigint REFERENCES ca.run_steps(id) ON DELETE CASCADE,
    level      text NOT NULL DEFAULT 'INFO',
    message    text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS run_events_run_idx ON ca.run_events (run_id, id);

--: what actually ran: selected procedure, its hash and its snapshot
CREATE TABLE IF NOT EXISTS ca.run_procedures (
    run_id         uuid NOT NULL REFERENCES ca.runs(id) ON DELETE CASCADE,
    stage          text NOT NULL,
    source_kind    text NOT NULL,
    source_ref     text NOT NULL DEFAULT '',
    procedure_id   uuid,
    code_hash      text NOT NULL DEFAULT '',
    code_snapshot  text NOT NULL DEFAULT '',
    params         jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (run_id, stage)
);

-- ---------------------------------------------------------------------------
-- datamart: normalized (vertical) + comparison (derived) + slices
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ca.normalized_rows (
    run_id        uuid NOT NULL REFERENCES ca.runs(id) ON DELETE CASCADE,
    app_id        text NOT NULL,
    source        text NOT NULL,               -- BEFORE | AFTER
    path          text NOT NULL,
    array_key     text NOT NULL DEFAULT '',
    prefix        text NOT NULL DEFAULT '',
    cls           text NOT NULL DEFAULT '',
    suffix        text NOT NULL DEFAULT '',
    key           text NOT NULL DEFAULT '',
    value         text NOT NULL,
    value_type    text NOT NULL DEFAULT 'string',
    adjusted_value text NOT NULL DEFAULT '',
    pointer       text NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, app_id, source, path, array_key)
) ;

CREATE TABLE IF NOT EXISTS ca.comparison_rows (
    run_id          uuid NOT NULL REFERENCES ca.runs(id) ON DELETE CASCADE,
    app_id          text NOT NULL,
    path            text NOT NULL,
    array_key       text NOT NULL DEFAULT '',
    prefix          text NOT NULL DEFAULT '',
    cls             text NOT NULL DEFAULT '',
    suffix          text NOT NULL DEFAULT '',
    key             text NOT NULL DEFAULT '',
    value_before    text,
    value_after     text,
    adjusted_before text,
    adjusted_after  text,
    diff_flag       smallint NOT NULL,
    change_type     text NOT NULL,
    present_before  boolean NOT NULL DEFAULT true,
    present_after   boolean NOT NULL DEFAULT true,
    value_type_before text,
    value_type_after  text,
    PRIMARY KEY (run_id, app_id, path, array_key)
);
CREATE INDEX IF NOT EXISTS cmp_path_flag_idx ON ca.comparison_rows (run_id, path, diff_flag);
CREATE INDEX IF NOT EXISTS cmp_type_idx      ON ca.comparison_rows (run_id, change_type);
CREATE INDEX IF NOT EXISTS cmp_app_idx       ON ca.comparison_rows (run_id, app_id);
CREATE INDEX IF NOT EXISTS cmp_path_ops_idx  ON ca.comparison_rows (run_id, path text_pattern_ops);
CREATE INDEX IF NOT EXISTS cmp_key_idx       ON ca.comparison_rows (run_id, key);
CREATE INDEX IF NOT EXISTS cmp_arraykey_idx  ON ca.comparison_rows (run_id, array_key);

CREATE TABLE IF NOT EXISTS ca.applications (
    run_id     uuid NOT NULL REFERENCES ca.runs(id) ON DELETE CASCADE,
    app_id     text NOT NULL,
    flags      jsonb NOT NULL DEFAULT '{}'::jsonb,
    cause_category text NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, app_id)
);

CREATE TABLE IF NOT EXISTS ca.app_segments (
    run_id             uuid NOT NULL REFERENCES ca.runs(id) ON DELETE CASCADE,
    app_id             text NOT NULL,
    decision_before    text NOT NULL DEFAULT '',
    decision_after     text NOT NULL DEFAULT '',
    decision_changed   smallint NOT NULL DEFAULT 0,
    products           text NOT NULL DEFAULT '',
    top_offer_before   text NOT NULL DEFAULT '',
    top_offer_after    text NOT NULL DEFAULT '',
    partner_type       text NOT NULL DEFAULT '',
    routing_method     text NOT NULL DEFAULT '',
    pd_model_before    text NOT NULL DEFAULT '',
    pd_model_after     text NOT NULL DEFAULT '',
    cutoff_before      text NOT NULL DEFAULT '',
    cutoff_after       text NOT NULL DEFAULT '',
    reject_reason_before text NOT NULL DEFAULT '',
    reject_reason_after  text NOT NULL DEFAULT '',
    score_okb_before   text NOT NULL DEFAULT '',
    score_okb_after    text NOT NULL DEFAULT '',
    reference_product_id text NOT NULL DEFAULT '',
    cause_category     text NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, app_id)
);
CREATE INDEX IF NOT EXISTS segs_decision_idx ON ca.app_segments (run_id, decision_changed);
CREATE INDEX IF NOT EXISTS segs_model_idx    ON ca.app_segments (run_id, pd_model_after);

CREATE TABLE IF NOT EXISTS ca.product_segments (
    run_id        uuid NOT NULL REFERENCES ca.runs(id) ON DELETE CASCADE,
    app_id        text NOT NULL,
    source        text NOT NULL,
    product_id    text NOT NULL,
    product_name  text NOT NULL DEFAULT '',
    partner_type  text NOT NULL DEFAULT '',
    routing_method text NOT NULL DEFAULT '',
    pd_model      text NOT NULL DEFAULT '',
    cutoff        text NOT NULL DEFAULT '',
    cutoff_value  text NOT NULL DEFAULT '',
    decision      text NOT NULL DEFAULT '',
    reject_reason text NOT NULL DEFAULT '',
    offer_rank    text NOT NULL DEFAULT '',
    offer_type    text NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, app_id, source, product_id)
);
CREATE INDEX IF NOT EXISTS pseg_product_idx ON ca.product_segments (run_id, product_id, app_id);

-- ---------------------------------------------------------------------------
-- facts for the pivot + their application membership
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ca.pivot_facts (
    run_id        uuid NOT NULL REFERENCES ca.runs(id) ON DELETE CASCADE,
    revision      integer NOT NULL DEFAULT 1,
    fact_id       bigserial,
    prefix        text NOT NULL DEFAULT '',
    cls           text NOT NULL DEFAULT '',
    suffix        text NOT NULL DEFAULT '',
    array_key     text NOT NULL DEFAULT '',
    key           text NOT NULL DEFAULT '',
    path          text NOT NULL DEFAULT '',
    value_adjusted text NOT NULL DEFAULT '',
    diff_flag     smallint NOT NULL DEFAULT 0,
    change_type   text NOT NULL DEFAULT '',
    source        text NOT NULL DEFAULT '',
    cnt           integer NOT NULL DEFAULT 0,
    PRIMARY KEY (fact_id)
);
CREATE INDEX IF NOT EXISTS facts_run_idx ON ca.pivot_facts (run_id, revision);

CREATE TABLE IF NOT EXISTS ca.pivot_membership (
    run_id    uuid NOT NULL REFERENCES ca.runs(id) ON DELETE CASCADE,
    revision  integer NOT NULL DEFAULT 1,
    fact_id   bigint NOT NULL REFERENCES ca.pivot_facts(fact_id) ON DELETE CASCADE,
    app_id    text NOT NULL,
    PRIMARY KEY (run_id, revision, fact_id, app_id)
);
CREATE INDEX IF NOT EXISTS memb_app_idx ON ca.pivot_membership (run_id, revision, app_id);

-- ---------------------------------------------------------------------------
-- checks
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ca.check_executions (
    id          bigserial PRIMARY KEY,
    run_id      uuid NOT NULL REFERENCES ca.runs(id) ON DELETE CASCADE,
    revision    integer NOT NULL DEFAULT 1,
    check_id    text NOT NULL,
    label       text NOT NULL DEFAULT '',
    source_kind text NOT NULL DEFAULT 'workspace',
    source_ref  text NOT NULL DEFAULT '',
    params      jsonb NOT NULL DEFAULT '{}'::jsonb,
    state       text NOT NULL DEFAULT 'PENDING',   -- PENDING|RUNNING|DONE|ERROR
    duration_ms integer NOT NULL DEFAULT 0,
    error       text,
    row_count   integer NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS exec_run_idx ON ca.check_executions (run_id, revision);

CREATE TABLE IF NOT EXISTS ca.check_results (
    id           bigserial PRIMARY KEY,
    execution_id bigint NOT NULL REFERENCES ca.check_executions(id) ON DELETE CASCADE,
    run_id       uuid NOT NULL REFERENCES ca.runs(id) ON DELETE CASCADE,
    revision     integer NOT NULL DEFAULT 1,
    label        text NOT NULL DEFAULT '',
    source       text NOT NULL DEFAULT '',
    argument     text NOT NULL DEFAULT '',
    cnt          integer NOT NULL DEFAULT 0,
    total        integer NOT NULL DEFAULT 0,
    metric       double precision,
    app_ids      text[] NOT NULL DEFAULT '{}',
    result       text NOT NULL DEFAULT '',
    confidence   text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS cres_run_idx ON ca.check_results (run_id, revision);

-- ---------------------------------------------------------------------------
-- LLM second opinion (never part of the deterministic result)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ca.llm_reports (
    id           bigserial PRIMARY KEY,
    run_id       uuid NOT NULL REFERENCES ca.runs(id) ON DELETE CASCADE,
    revision     integer NOT NULL DEFAULT 1,
    provider     text NOT NULL DEFAULT '',
    model        text NOT NULL DEFAULT '',
    payload_hash text NOT NULL DEFAULT '',
    state        text NOT NULL DEFAULT 'PENDING',  -- PENDING|RUNNING|DONE|UNAVAILABLE|ERROR
    result       jsonb,
    error        text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz
);
CREATE INDEX IF NOT EXISTS llm_run_idx ON ca.llm_reports (run_id, revision);

-- ---------------------------------------------------------------------------
-- migration bookkeeping (created by the runner, restated here for clarity)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ca.schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
