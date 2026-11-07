# CONTRACTS.md — approved interfaces (v1)

> Coordinator-owned. Every agent codes against this file. If you need to change
> a signature, message the coordinator **before** editing.
> Written 2026-11-07. Numbers in here are verified facts, not intentions.

## 0. Verified facts about the data (do not re-derive)

The pipeline in `src/change_analysis/pipeline/` is **already validated against the
organizers' own reference** (`tools/validate_against_reference.py`):

| Check | Our result | Reference | |
|---|---|---|---|
| comparison rows (OPEN, 2002 apps) | 289 533 | 289 533 | ✅ |
| apps with any difference | 502 | 502 | ✅ |
| apps with decision flip | 385 | 385 | ✅ |
| all segment fields vs `expected_OPEN.csv` | 0 mismatches | — | ✅ |
| HOLDOUT (998 apps) changed / decision-changed | 248 / 186 | 248 / 186 | ✅ |
| pipeline wall clock | ~3.7 s | 4.6 s naive | |

`pipeline/core.py` and `pipeline/segments.py` are **frozen**: do not edit without
coordinator approval. They have no dependency on anything but the stdlib.

### Serialization contract (public — the delivered checks depend on it)

| input | `VALUE` | `VALUE_TYPE` |
|---|---|---|
| `true` / `false` | `true` / `false` | boolean |
| JSON `null` | `NULL` | null |
| `""` | `` (empty) | string |
| the string `"NULL"` | `NULL` | string |
| number | `710`, `0.55`, integral floats collapse to `710` | number |
| attribute absent | *(no row)* | — |

`PATH` = dotted path with array markers removed, e.g.
`decisionResult.product_trials.rules.hit`.
`ARRAY_KEY` = element keys of **all** arrays on the path, outer→inner, `|`-joined,
e.g. `15210|AGELOW@21`, `ML_MICRO_LOAN_V1|0`.

## 1. File ownership (disjoint write scopes)

| Owner | Paths |
|---|---|
| **coordinator (lead)** | `src/change_analysis/{config.py,contracts/,pipeline/,worker/,procedures/,services/,checks/}`, `migrations/`, `docs/`, `README.md`, `tests/`, `tools/`, `pyproject.toml`, `.env.example` |
| **db-core** | `src/change_analysis/{db/,auth/}`, `tests/integration/db_*.py` |
| **ui-app** | `src/change_analysis/ui/`, `main.py`, `tests/e2e/ui_*.py` |
| **reports** | `src/change_analysis/{reports/,llm/}`, `src/change_analysis/templates/`, `src/change_analysis/static/`, `tests/unit/reports_*.py` |

Two agents never write the same file. `docs/agent_reports/<role>.md` is each
agent's own scratch report (report format in PLAN.md §23.3).

## 2. `db/` — connection, migrations, MartAPI  (owner: db-core)

```python
# src/change_analysis/db/connection.py
def get_pool() -> psycopg_pool.ConnectionPool      # module-level singleton, lazy
def dsn() -> str                                    # from config.load_settings()
@contextmanager
def tx() -> Iterator[psycopg.Connection]            # one short transaction, commits on exit
def query(sql: str, params: Sequence[Any] = ()) -> list[dict]     # read helper
def execute(sql: str, params: Sequence[Any] = ()) -> int          # write helper
def copy_rows(table: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> int
```

```python
# src/change_analysis/db/migrations.py
def apply_migrations() -> list[str]     # applies migrations/*.sql in name order, idempotent
```

```python
# src/change_analysis/db/mart.py
class Mart:
    """Bound to exactly one run_id. Implements checks/README.md exactly."""
    def __init__(self, run_id: str) -> None: ...
    def rows(self, **filters) -> Iterator[dict]
    def count(self, **filters) -> int
    def app_ids(self, **filters) -> list[str]      # sorted, unique, str
    def segments(self) -> Iterator[dict]
    def total_apps(self) -> int
```

`rows()` yields dicts with exactly these keys:
`APP_ID, PREFIX, CLASS, SUFFIX, ARRAY_KEY, KEY, PATH, VALUE_BEFORE, VALUE_AFTER,
ADJUSTED_BEFORE, ADJUSTED_AFTER, DIFF_FLAG, CHANGE_TYPE`.
`segments()` yields exactly:
`APP_ID, DECISION_BEFORE, DECISION_AFTER, DECISION_CHANGED, PRODUCTS,
TOP_OFFER_BEFORE, TOP_OFFER_AFTER, PARTNER_TYPE, ROUTING_METHOD,
PD_MODEL_BEFORE, PD_MODEL_AFTER, CUTOFF_BEFORE, CUTOFF_AFTER,
REJECT_REASON_BEFORE, REJECT_REASON_AFTER, SCORE_OKB_BEFORE, SCORE_OKB_AFTER`.

Hard rules (they are graded):
* `DECISION_CHANGED` is `int` 0/1, never the string `"1"`.
* `DIFF_FLAG` is `int` 0/1.
* `VALUE_BEFORE`/`VALUE_AFTER` are `None` where the attribute is absent.
* `app_ids=[]` ⇒ empty sample, **not** "no filter". Same for `path=[]`,
  `change_type=[]`.
* `path_prefix` is a **literal** prefix: `%` and `_` inside it are escaped.
* Unknown filter kwarg ⇒ `ValueError` with the name in the message.
* `count()` counts rows, `app_ids()` counts distinct applications.
* `total_apps()` = number of processed pairs, **not** row count.

## 3. `checks/` — loading and executing user checks  (owner: db-core)

```python
# src/change_analysis/checks/loader.py
@dataclass(frozen=True)
class CheckSpec:
    id: str
    label: str
    params: dict[str, Any]
    source_kind: str        # 'workspace' | 'file' | 'builtin'
    source_ref: str         # procedure id / file path / builtin path
    code: str

def list_builtin_checks() -> list[CheckSpec]           # the 5 delivered files, unmodified
def load_from_source(code: str, source_ref: str, source_kind: str = "workspace") -> CheckSpec
def load_from_file(path: str) -> CheckSpec             # refuses paths outside SCRIPT_ROOT / checks dir

class CheckError(RuntimeError): ...

def execute(spec: CheckSpec, run_id: str, overrides: dict | None = None) -> dict
    # returns {"state": "DONE"|"ERROR", "rows": [validated dicts], "error": str, "duration_ms": int}
```

`execute()` (not the caller) instantiates `Mart(run_id)`.

Row validation, applied to **every** returned row, in `checks/loader.py::validate_rows`:
`SOURCE ∈ {BEFORE, AFTER}`; `RESULT ∈ {PASS, FAIL}`; `CONFIDENCE ∈ {HIGH, LOW}`;
`CNT`/`TOTAL` ≥ 0; `APP_IDS` a list of `str` restricted to the run; `METRIC` is a
plain number (`None` allowed). A technical failure is `state=ERROR` — never a
business `RESULT=FAIL`. An empty list is a legal "executed, 0 rows" result.

The five delivered files must be executed **without modification**, from
`<task package>/checks/`.

## 4. `auth/`  (owner: db-core)

```python
# src/change_analysis/auth/service.py
def create_user(login: str, password: str, display_name: str = "") -> str      # user_id
def authenticate(login: str, password: str) -> str | None                       # user_id
def create_session(user_id: str) -> str                                         # opaque token
def user_for_session(token: str | None) -> dict | None   # {id, login, display_name}
def drop_session(token: str) -> None
def ensure_user(login: str, password: str, display_name: str = "") -> str
```
Argon2 (`argon2.PasswordHasher`) — never hand-rolled crypto. Sessions live in
`ca.sessions` with `expires_at`. Owner checks happen in `services/`, not here.

## 5. `services/` — the only API the UI may call  (owner: coordinator)

Everything returns plain dicts / dataclasses, never psycopg rows.
Every function that touches a run/config/procedure takes `user_id` **first** and
must raise `AccessDenied` (defined in `services/errors.py`) when the object
belongs to somebody else. Hiding a button is not an owner check.

```python
# services/datasets.py
list_datasets(user_id) -> list[dict]
register_dataset(user_id, name, root_ref) -> str
precheck_dataset(root_ref) -> dict   # {pairs, before, after, manifest, errors[], warnings[]}

# services/configs.py
list_configurations(user_id) -> list[dict]
save_configuration(user_id, name, dataset_id, selections, params, config_id=None) -> str
load_configuration(user_id, config_id) -> dict

# services/procedures.py
list_procedures(user_id, stage=None) -> list[dict]
save_procedure(user_id, stage, name, source_kind, code, path_ref, params, procedure_id=None) -> str
list_registered_files() -> list[dict]          # .py under SCRIPT_ROOT
read_registered_file(rel_path) -> str

# services/runs.py
create_run(user_id, name, dataset_id, selections, params, backend="cpu",
           idempotency_key=None) -> str        # run_id; QUEUED; enqueues PIPELINE job
request_checks_only(user_id, run_id, check_specs, params=None) -> str   # job id
list_runs(user_id) -> list[dict]
get_run(user_id, run_id) -> dict               # raises AccessDenied
run_stages(user_id, run_id) -> list[dict]      # stage log + procedure ref for "jump to code"
run_events(user_id, run_id, after_id=0, limit=200) -> list[dict]
cancel_or_retry(user_id, run_id) -> str        # explicit retry => new job, same run

# services/report.py
summary(user_id, run_id, filters: FilterSpec) -> dict
pivot(user_id, run_id, dims_rows: list[str], dims_cols: list[str],
      filters: FilterSpec, measures=("apps",)) -> dict
      # {"row_dim_values": [...], "col_dim_values": [...], "cells": {"r|r|c|c": {"cnt":..}}, "grand_total": int}
drill(user_id, run_id, dims_rows, dims_cols, filters, cell_key, page=1, page_size=50) -> dict
      # {"app_ids": [...], "total": int, "page": int, "page_size": int}
apps_page(user_id, run_id, filters, page=1, page_size=50, sort="APP_ID") -> dict
app_detail(user_id, run_id, app_id) -> dict    # segments both slices + product rows + cause flags
attribute_rows(user_id, run_id, app_id, include_same=False) -> list[dict]
raw_json(user_id, run_id, app_id, source) -> dict
check_report(user_id, run_id, revision=None) -> dict
export_html(user_id, run_id, revision=None) -> str   # absolute path of the generated file
```

`pivot()` must be exact: the measured value is `COUNT(DISTINCT app_id)` over the
facts matching the cell, and drill-down must return exactly the same set.

Performance targets (measured, not aspired): pivot over the full run ≤ 300 ms,
drill-down ≤ 300 ms, `pivot_facts` 1 query.

## 6. `ui/`  (owner: ui-app)

NiceGUI 4.0. Pages, all owner-checked server side:

| route | page |
|---|---|
| `/login` | login form, no registration needed for the demo |
| `/` | run list: own runs, status, pairs, open link; empty state |
| `/configs` | configuration editor: dataset, three procedures × two sources, checks + params, backend, save / run |
| `/workspace` | Python workspace: list of procedures & checks, CodeMirror, params JSON, save / execute, stdout/errors/result rows |
| `/runs/{run_id}` | live stage log (1 s polling), progress, jump-to-code per stage, final summary + links |
| `/runs/{run_id}/report` | checks table (one merged table) + HTML download link |
| `/runs/{run_id}/explore` | pivot + drill-down + application card + attribute table + raw JSON |

Rules: no long computation in an event handler (everything goes to the worker);
`app.storage.user` holds only UI preferences, never authorisation; after a page
refresh the state is rebuilt from the database (PLAN.md R20).

## 7. `reports/` and `llm/`  (owner: reports)

```python
# reports/summary.py
build_summary(run_id: str, filters: FilterSpec) -> dict   # the 8 cards of PLAN.md §15.5

# reports/pivot.py        -- pure functions over mart/service results, no SQL
def build_matrix(cells, row_dim_values, col_dim_values) -> ...

# reports/export.py
export_report(run_id: str, out_path: str, base_url: str) -> str   # Jinja2, standalone HTML
```

HTML export: inline CSS, no external CDN, every user-provided value escaped with
`markupsafe.escape`, absolute `PUBLIC_BASE_URL` links back to the app.

```python
# llm/adapter.py
def status() -> dict                 # {available: bool, provider, model, reason}
def request_second_opinion(run_id: str, revision: int, payload: dict) -> dict
    # {state: DONE|UNAVAILABLE|ERROR, summary, observations[], uncertainty[], error}
```
The LLM block **never** changes deterministic numbers and **never** fabricates an
answer when no key is configured: it reports `UNAVAILABLE` with a reason.

## 8. Procedures (`procedures/`) — owner: coordinator

Two sources, one loader. Registry file `procedures/REGISTRY` is implicit: any
`.py` under `SCRIPT_ROOT` is a candidate.

```python
PROCEDURE = {
    "id": "spr_normalize_v1",
    "label": "СПР response normalization",
    "stage": "normalize",              # normalize | compare | aggregate
    "contract_version": 1,
    "params": {...defaults...},
    "entrypoint": run,
}
def run(context, inputs, params): ...
```

`context` is a plain object with: `run_id`, `backend`, `log(msg)`, `progress(done, total)`,
`artifact_dir` (read/write), and **no** database, shell or secrets.
`inputs` per stage is defined in `procedures/README.md`.
`params` = procedure defaults ⊕ configuration settings (no `eval`).

## 9. Environment

* Python: `app/.venv` (3.14.7), NiceGUI 4.0.0, psycopg 3.3.8, pandas 3.1.1.
* PostgreSQL 18.6 runs **project-local** on port **55432** with data dir
  `app/.pgdata` (see `scripts/pg.sh`), because this machine's system cluster does
  not have a role for our user. `DATABASE_URL` is in `.env.example`.
* GPU: NVIDIA GTX 1650 Ti, 4 GB — cudf is **not** installed yet; the GPU path is
  a measured experiment, never a claim. CPU is the required mode.
