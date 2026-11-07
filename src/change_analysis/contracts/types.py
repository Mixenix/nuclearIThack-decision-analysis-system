"""Approved contracts — the single source of truth for every agent.

Changing anything in this module requires coordinating with the coordinator
(PLAN.md §11.2, §23.2).  Every other module imports from here; nothing here
imports from the application.

Dataclasses only (no schema framework) so that the same types can travel
between the web process, the worker and the compute subprocess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

CONTRACT_VERSION = 1

# ---------------------------------------------------------------------------
# stages / states
# ---------------------------------------------------------------------------

STAGES: tuple[str, ...] = ("normalize", "compare", "aggregate", "checks")
PROCEDURE_STAGES: tuple[str, ...] = ("normalize", "compare", "aggregate")

SOURCE_BEFORE = "BEFORE"
SOURCE_AFTER = "AFTER"
SOURCES = (SOURCE_BEFORE, SOURCE_AFTER)

RUN_QUEUED = "QUEUED"
RUN_RUNNING = "RUNNING"
RUN_SUCCEEDED = "SUCCEEDED"
RUN_FAILED = "FAILED"
RUN_INTERRUPTED = "INTERRUPTED"

STEP_PENDING = "PENDING"
STEP_RUNNING = "RUNNING"
STEP_DONE = "DONE"
STEP_FAILED = "FAILED"

JOB_KINDS = ("PIPELINE", "CHECKS_ONLY", "LLM")

CHANGE_TYPES = ("SAME", "VALUE_CHANGED", "ATTRIBUTE_NEW", "ATTRIBUTE_LOST")


# ---------------------------------------------------------------------------
# TableRef — a large table by reference, never by value (PLAN.md §11.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TableRef:
    run_id: str
    kind: str                 # normalized | comparison | segments | facts
    schema_version: int = CONTRACT_VERSION
    row_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "kind": self.kind,
                "schema_version": self.schema_version, "row_count": self.row_count}

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> "TableRef":
        return TableRef(str(d["run_id"]), str(d["kind"]),
                        int(d.get("schema_version", CONTRACT_VERSION)),
                        int(d.get("row_count", 0)))


# ---------------------------------------------------------------------------
# FilterSpec — the single filter model shared by UI, mart and reports
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FilterSpec:
    """Serialisable sample filter.

    Every field is optional; ``None`` means "no constraint".  An *empty list*
    means "empty sample" (PLAN.md §12.1) — it is not the same as ``None``.
    """

    run_id: str = ""
    products: tuple[str, ...] | None = None
    partners: tuple[str, ...] | None = None
    decision_changed: bool | None = None
    change_types: tuple[str, ...] | None = None
    paths: tuple[str, ...] | None = None
    path_prefix: str | None = None
    source: str | None = None
    cause_categories: tuple[str, ...] | None = None
    app_ids: tuple[str, ...] | None = None
    diff_flag: int | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"run_id": self.run_id}
        for name in ("products", "partners", "change_types", "paths",
                     "cause_categories", "app_ids"):
            value = getattr(self, name)
            if value is not None:
                out[name] = list(value)
        for name in ("decision_changed", "path_prefix", "source", "diff_flag"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        return out

    @staticmethod
    def from_dict(d: Mapping[str, Any] | None) -> "FilterSpec":
        d = dict(d or {})
        lists = {}
        for name in ("products", "partners", "change_types", "paths",
                     "cause_categories", "app_ids"):
            value = d.get(name)
            lists[name] = None if value is None else tuple(str(v) for v in value)
        def _opt(name: str):
            return d.get(name)
        dc = _opt("decision_changed")
        df = _opt("diff_flag")
        return FilterSpec(
            run_id=str(d.get("run_id", "")),
            products=lists["products"], partners=lists["partners"],
            decision_changed=None if dc is None else bool(dc),
            change_types=lists["change_types"], paths=lists["paths"],
            path_prefix=_opt("path_prefix"),
            source=_opt("source"),
            cause_categories=lists["cause_categories"],
            app_ids=lists["app_ids"],
            diff_flag=None if df is None else int(df),
        )

    def is_empty_sample(self) -> bool:
        return self.app_ids is not None and len(self.app_ids) == 0


# ---------------------------------------------------------------------------
# procedure results (PLAN.md §11.2)
# ---------------------------------------------------------------------------


@dataclass
class Diagnostic:
    level: str = "INFO"            # INFO | WARNING | ERROR
    message: str = ""
    code: str = ""
    where: str = ""                # file / path / app_id

    def as_dict(self) -> dict[str, Any]:
        return {"level": self.level, "message": self.message,
                "code": self.code, "where": self.where}


@dataclass
class NormalizeResult:
    """Output of the normalize stage: flat rows for both slices."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    segments: list[dict[str, Any]] = field(default_factory=list)
    product_segments: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)


@dataclass
class CompareResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)


@dataclass
class AggregateResult:
    """Facts for the pivot + the application ids behind every fact."""

    facts: list[dict[str, Any]] = field(default_factory=list)
    membership: list[tuple[int, str]] = field(default_factory=list)  # (fact_index, app_id)
    summary: dict[str, Any] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)


# ---------------------------------------------------------------------------
# CHECK contract (task package checks/README.md) — do not alter
# ---------------------------------------------------------------------------

CHECK_RESULT_FIELDS: tuple[str, ...] = (
    "LABEL", "SOURCE", "ARGUMENT", "CNT", "TOTAL", "METRIC",
    "APP_IDS", "RESULT", "CONFIDENCE",
)

CHECK = {"id": str, "label": str, "params": dict, "entrypoint": object}


@dataclass
class CheckExecution:
    """Technical state of running one check — never mixed with PASS/FAIL."""

    check_id: str
    state: str = "PENDING"          # PENDING | RUNNING | DONE | ERROR
    duration_ms: int = 0
    error: str = ""
    rows: int = 0


# ---------------------------------------------------------------------------
# pivot dimensions
# ---------------------------------------------------------------------------

#: dimensions a user may place on rows / columns / filters
PIVOT_DIMENSIONS: tuple[str, ...] = (
    "PREFIX", "CLASS", "SUFFIX", "ARRAY_KEY", "KEY", "PATH",
    "VALUE_ADJUSTED", "DIFF_FLAG", "CHANGE_TYPE", "SOURCE",
)
