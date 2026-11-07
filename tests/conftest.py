"""Fixtures for the acceptance suite of «Система анализа изменений СПР».

Isolation rules (the shared cluster is used by other agents, so these are hard
requirements, not preferences):

* every test that writes to PostgreSQL runs against ``TEST_DATABASE_URL``
  (default ``postgresql://ca:ca@127.0.0.1:55432/change_analysis_test``); the URL
  is forced into ``os.environ`` *before* ``change_analysis`` is imported, because
  ``config.load_settings()`` lets the environment win over ``app/.env``;
* runs created by the suite are named ``qa-...`` and are deleted by the fixture
  that created them, with ``WHERE id = ANY(<exactly those ids>)``;
* :func:`qa_support.assert_test_database` runs before every destructive helper
  and refuses to touch anything that is not the test database.

The heavy fixture is :func:`completed_run`: it drives the *real* pipeline
(``worker.runner.PipelineJob``) instead of a mock, so the numbers the suite
asserts are the numbers the worker produces.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = APP_DIR / "src"
TESTS_DIR = Path(__file__).resolve().parent
for _candidate in (str(SRC_DIR), str(TESTS_DIR)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

#: throw-away database; nothing in this suite may write to the demo database
TEST_DSN = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://ca:ca@127.0.0.1:55432/change_analysis_test"
)
os.environ["DATABASE_URL"] = TEST_DSN
#: keep generated reports out of the artifacts of the live application
os.environ.setdefault("ARTIFACT_ROOT", tempfile.mkdtemp(prefix="qa-artifacts-"))

#: the run name carries a payload on purpose: the HTML export must show it as
#: text (PLAN.md §21.3 «HTML с пользовательским <script> показывает текст»)
RUN_NAME = "qa-прогон <script>alert(1)</script>"
INJECTION = "<script>alert(1)</script>"


def pytest_configure(config: pytest.Config) -> None:
    # A hung test used to hang the whole suite (a pivot query waited for minutes on
    # a bloated test database).  Fail loudly instead of waiting for ever.
    # ``-p no:timeout`` disables this; the plugin ships in app/.venv.
    if config.pluginmanager.hasplugin("timeout"):
        config.option.timeout = float(os.environ.get("QA_TEST_TIMEOUT", "240"))
        config.option.timeout_method = "thread"
    config.addinivalue_line("markers", "unit: no database, no network")
    config.addinivalue_line("markers", "integration: requires PostgreSQL")
    config.addinivalue_line("markers", "e2e: drives the pipeline or the report cycle")


# ---------------------------------------------------------------------------
# database / environment
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def settings():
    from change_analysis.config import load_settings

    loaded = load_settings()
    assert loaded.database_url == TEST_DSN, (
        "the suite must run against TEST_DATABASE_URL, "
        f"got {loaded.database_url!r}"
    )
    return loaded


@pytest.fixture(scope="session")
def test_database(settings):
    """Prepare the throw-away database and leave it clean.

    Deliberately **not** ``autouse``: unit tests must run without a database at
    all, so the fixture is pulled in only by the tests (and fixtures) that use it.
    

    Runs that a killed test left behind are removed (only rows named ``qa-...``),
    and the planner statistics are refreshed: without statistics a pivot over a
    bloated table takes minutes instead of a second, which used to look like a
    hang in the middle of the suite.
    """
    from change_analysis.db.connection import close_pool, execute
    from change_analysis.db.migrations import apply_migrations
    from qa_support import assert_test_database

    assert_test_database(settings.database_url)
    applied = apply_migrations()
    # ``%`` cannot appear unescaped in a parameterised psycopg query, so the
    # prefix test is expressed as a regular expression.
    #
    # Only runs that existed *before* this session started are removed: two test
    # sessions on the same throw-away database used to delete each other's runs
    # mid-flight, which surfaced as a foreign-key error in ``run_events`` and
    # looked like a broken pipeline.
    stale = execute(
        "DELETE FROM ca.runs WHERE name ~ '^qa-' AND created_at < now() - interval '2 seconds'")
    for table in ("comparison_rows", "pivot_facts", "pivot_membership",
                  "app_segments", "product_segments"):
        execute(f"ANALYZE ca.{table}")
    yield {"applied": applied, "stale_runs_removed": stale}
    close_pool()


@pytest.fixture(scope="session")
def owner(test_database) -> str:
    from change_analysis.auth import service as auth

    return auth.ensure_user("qa-tester", "qa-tester", "QA-тестировщик")


@pytest.fixture(scope="session")
def other_owner(test_database) -> str:
    from change_analysis.auth import service as auth

    return auth.ensure_user("qa-tester-2", "qa-tester-2", "QA-второй пользователь")


@pytest.fixture(scope="session")
def dataset_id(test_database, owner) -> str:
    from change_analysis.services import datasets

    registered = datasets.ensure_delivered_dataset(owner)
    assert registered, "the delivered dataset under DATASET_ROOT was not found"
    return registered


# ---------------------------------------------------------------------------
# the cheap synthetic run (filter escaping, checks-only, isolation)
# ---------------------------------------------------------------------------

_SYNTHETIC_CLEANUP: list[str] = []


def _delete_runs(run_ids: list[str]) -> None:
    from change_analysis.config import load_settings
    from change_analysis.db.connection import execute
    from qa_support import assert_test_database

    if not run_ids:
        return
    assert_test_database(load_settings().database_url)
    execute("DELETE FROM ca.runs WHERE id = ANY(%s)", (run_ids,))


@pytest.fixture
def synthetic_run(test_database, owner, dataset_id):
    """Factory: a SUCCEEDED run whose mart tables are seeded explicitly.

    Used where a full 2002-application run would be wasteful *and* where the test
    needs a shape the delivered dataset does not have (a path containing ``%`` or
    ``_``, no approved applications at all).  The run is deleted afterwards.
    """
    from change_analysis.db.connection import execute, tx
    from qa_support import insert_comparison_rows, insert_facts, insert_segments

    created: list[str] = []

    def build(name: str = "qa-synthetic", rows=(), segments=(), facts=(),
              revision: int = 1, state: str = "SUCCEEDED",
              extra_runs: int = 1) -> list[str]:
        ids: list[str] = []
        for index in range(extra_runs):
            with tx() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO ca.runs (owner_id, dataset_id, name, state, backend,"
                    " expected_pairs, processed_pairs, revision, summary)"
                    " VALUES (%s,%s,%s,%s,'cpu',%s,%s,%s,'{}'::jsonb) RETURNING id",
                    (owner, dataset_id, f"{name}-{index + 1}" if extra_runs > 1 else name,
                     state, 0, 0, revision),
                )
                ids.append(str(cur.fetchone()["id"]))
        run_id = ids[0]
        seed = [dict(row, run_id=run_id) for row in rows]
        insert_comparison_rows(execute, seed)
        insert_segments(execute, [dict(row, run_id=run_id) for row in segments])
        if facts:
            insert_facts(tx, run_id, revision, facts)
        created.extend(ids)
        return ids if extra_runs > 1 else run_id

    yield build
    _delete_runs(created)


@pytest.fixture
def new_run(test_database, owner, dataset_id):
    """Factory: a QUEUED run (real ``services.runs.create_run``), deleted afterwards."""
    from change_analysis.services import runs

    created: list[str] = []

    def build(name: str = "qa-queued", selections=None, idempotency_key: str | None = None,
              user_id: str | None = None) -> str:
        run_id = runs.create_run(
            user_id or owner, name, dataset_id, selections or {},
            idempotency_key=idempotency_key or f"qa-{uuid.uuid4()}",
        )
        created.append(run_id)
        return run_id

    yield build
    _delete_runs(created)


# ---------------------------------------------------------------------------
# the expensive fixture: one real full run
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def completed_run(settings, test_database, owner, dataset_id) -> dict:
    """A real full run over the delivered dataset, driven by the real pipeline.

    The run is created through ``services.runs.create_run`` and executed with
    ``worker.runner.PipelineJob`` — the same code the worker process runs, minus
    the queue claim.  It is deleted at the end of the session.
    """
    from change_analysis.checks.loader import list_builtin_checks
    from change_analysis.db.connection import query
    from change_analysis.services import runs
    from change_analysis.worker.runner import PipelineJob

    checks = list_builtin_checks()
    assert len(checks) == 5, f"expected the five delivered checks, loaded {len(checks)}"
    selections = {"checks": [{"id": spec.id, "source": "builtin"} for spec in checks]}
    run_id = runs.create_run(
        owner, RUN_NAME, dataset_id, selections,
        idempotency_key=f"qa-session-run-{uuid.uuid4()}",
    )
    job = query("SELECT id, run_id FROM ca.jobs WHERE run_id = %s", (run_id,))[0]
    started = time.perf_counter()
    PipelineJob({"id": int(job["id"]), "run_id": run_id}, settings).run()
    duration = time.perf_counter() - started

    run = runs.get_run(owner, run_id)
    assert run["state"] == "SUCCEEDED", (
        f"the pipeline did not succeed: {run['state']} {run.get('error_message')!r}"
    )
    from change_analysis.db.connection import execute as _execute

    for table in ("comparison_rows", "pivot_facts", "pivot_membership",
                  "app_segments", "product_segments", "check_results"):
        _execute(f"ANALYZE ca.{table}")

    payload = {
        "run_id": run_id,
        "owner": owner,
        "name": RUN_NAME,
        "duration_sec": duration,
        "summary": dict(run["summary"] or {}),
        "check_ids": [spec.id for spec in checks],
    }
    yield payload
    _delete_runs([run_id])
