"""Loading and validating user checks (``checks/README.md`` contract).

A check is never imported into the web process: the worker executes it in a
separate interpreter under a read-only database role.  This module only decides
what the code is, that it looks like a check, and whether its output is legal.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import sys
import time
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import load_settings

CHECK_KEYS = {"id", "label", "params", "entrypoint"}


class CheckError(ValueError):
    pass


@dataclass(frozen=True)
class CheckSpec:
    id: str
    label: str
    params: dict[str, Any]
    source_kind: str
    source_ref: str
    code: str
    entrypoint: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "label": self.label, "params": self.params,
            "source_kind": self.source_kind, "source_ref": self.source_ref,
        }


def _module_name(source: str) -> str:
    return "user_check_" + hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]


def _load_module_from_source(code: str, source_ref: str) -> types.ModuleType:
    name = _module_name(code + source_ref)
    module = types.ModuleType(name)
    module.__dict__["__name__"] = name
    try:
        compiled = compile(code, source_ref or "<check>", "exec")
    except SyntaxError as exc:
        raise CheckError(f"syntax error in check {source_ref}: {exc}") from exc
    sys.modules[name] = module
    try:
        exec(compiled, module.__dict__)  # noqa: S102 - this is the documented contract
    except Exception as exc:  # noqa: BLE001
        sys.modules.pop(name, None)
        raise CheckError(f"cannot import check {source_ref}: {type(exc).__name__}: {exc}") from exc
    return module


def _load_module_from_file(path: Path) -> types.ModuleType:
    name = _module_name(str(path))
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise CheckError(f"cannot load check file {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001
        sys.modules.pop(name, None)
        raise CheckError(f"cannot import check file {path}: {type(exc).__name__}: {exc}") from exc
    return module


def _spec_from_module(module: types.ModuleType, *, code: str, source_kind: str,
                      source_ref: str) -> CheckSpec:
    check = getattr(module, "CHECK", None)
    if not isinstance(check, dict):
        raise CheckError(
            f"{source_ref}: the file must export CHECK = {{'id','label','params','entrypoint'}}"
        )
    missing = CHECK_KEYS - set(check)
    if missing:
        raise CheckError(f"{source_ref}: CHECK is missing {sorted(missing)}")
    if not callable(check["entrypoint"]):
        raise CheckError(f"{source_ref}: CHECK['entrypoint'] is not callable")
    params = check.get("params") or {}
    if not isinstance(params, dict):
        raise CheckError(f"{source_ref}: CHECK['params'] must be a dict")
    return CheckSpec(
        id=str(check["id"]), label=str(check["label"]), params=dict(params),
        source_kind=source_kind, source_ref=source_ref, code=code,
        entrypoint=check["entrypoint"],
    )


def load_from_source(code: str, source_ref: str, source_kind: str = "workspace") -> CheckSpec:
    module = _load_module_from_source(code, source_ref)
    return _spec_from_module(module, code=code, source_kind=source_kind,
                             source_ref=source_ref)


def _allowed_roots() -> list[Path]:
    settings = load_settings()
    return [settings.script_root.resolve(), settings.checks_dir.resolve()]


def load_from_file(path: str | Path) -> CheckSpec:
    candidate = Path(path)
    if not candidate.is_absolute():
        roots = _allowed_roots()
        candidate = next((r / candidate for r in roots if (r / candidate).exists()),
                         roots[0] / candidate)
    resolved = candidate.resolve()
    if not any(resolved == root or root in resolved.parents for root in _allowed_roots()):
        raise CheckError(
            f"check file {resolved} is outside SCRIPT_ROOT and the delivered checks directory"
        )
    if not resolved.exists():
        raise CheckError(f"check file not found: {resolved}")
    module = _load_module_from_file(resolved)
    return _spec_from_module(module, code=resolved.read_text(encoding="utf-8"),
                             source_kind="file", source_ref=str(resolved))


def list_builtin_checks() -> list[CheckSpec]:
    """The five delivered checks, loaded from the task package unmodified."""
    settings = load_settings()
    checks: list[CheckSpec] = []
    for path in sorted(settings.checks_dir.glob("check_*.py")):
        try:
            checks.append(load_from_file(path))
        except CheckError:
            continue
    return checks


def _metadata_from_source(code: str, source_ref: str) -> tuple[str, str, dict[str, Any]] | None:
    """Read ``CHECK = {...}`` **without executing the file**.

    Listing the checks a user may pick from must not run that user's code in the
    web process: a heavy top-level import would freeze every open session for the
    duration of a page build (PLAN.md §23.1: no user code in the UI process).
    Execution still happens later, in the worker's separate interpreter, through
    :func:`load_from_source` / :func:`load_from_file`.
    """
    try:
        tree = ast.parse(code, filename=source_ref or "<check>")
    except SyntaxError:
        return ("", "", {})
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if not any(isinstance(t, ast.Name) and t.id == "CHECK" for t in targets):
            continue
        if not isinstance(value, ast.Dict):
            return ("", "", {})
        literal: dict[str, Any] = {}
        for key_node, val_node in zip(value.keys, value.values):
            if key_node is None:
                continue
            try:
                key = ast.literal_eval(key_node)
            except (ValueError, SyntaxError):
                continue
            try:
                literal[str(key)] = ast.literal_eval(val_node)
            except (ValueError, SyntaxError):
                continue
        if "id" not in literal or "label" not in literal:
            return ("", "", {})
        params = literal.get("params")
        return (str(literal["id"]), str(literal["label"]),
                dict(params) if isinstance(params, dict) else {})
    return None


def list_script_checks() -> list[CheckSpec]:
    """Registered ``.py`` checks, described **without importing them**.

    A file whose ``CHECK`` literal cannot be read statically is still listed (so
    the user sees it and can fix it), but under its file name and without
    parameters — never by executing it here.
    """
    settings = load_settings()
    out: list[CheckSpec] = []
    for path in sorted(settings.script_root.glob("*.py")):
        try:
            code = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = _metadata_from_source(code, str(path))
        if meta is None:
            continue  # no CHECK declaration: e.g. a normalisation procedure file
        if not meta[0]:
            spec = CheckSpec(id=path.stem,
                             label=f"{path.name} (CHECK не читается без запуска)",
                             params={}, source_kind="file", source_ref=str(path), code=code)
        else:
            check_id, label, params = meta
            spec = CheckSpec(id=check_id, label=label, params=params,
                             source_kind="file", source_ref=str(path), code=code)
        if spec.id not in {c.id for c in out}:
            out.append(spec)
    return out


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _count_field(value: Any, name: str, where: str) -> int:
    """CNT/TOTAL are counts of applications: 1.9 is a broken check, not 1.

    The contract (``checks/README.md``) says these are quantities of applications.
    Truncating a fractional value produced plausible-looking numbers from a
    nonsense answer, which is worse than refusing the row.
    """
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        raise CheckError(f"{where}: {name} must be a number, got a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise CheckError(f"{where}: {name} must be a whole number, got {value!r}")
    text = _as_str(value).strip()
    try:
        return int(text)
    except ValueError as exc:
        raise CheckError(f"{where}: {name} must be a whole number, got {text!r}") from exc


def validate_rows(rows: Any, run_id: str, known_app_ids: set[str] | None = None) -> list[dict]:
    """Validate the rows a check returned, with errors that name the field."""
    if rows is None:
        return []
    if isinstance(rows, dict):
        raise CheckError("a check must return a list of dicts, not a single dict")
    if not isinstance(rows, (list, tuple)):
        raise CheckError(f"a check must return a list, got {type(rows).__name__}")
    out: list[dict] = []
    for index, row in enumerate(rows):
        where = f"row {index}"
        if not isinstance(row, dict):
            raise CheckError(f"{where}: expected a dict, got {type(row).__name__}")
        for field in ("LABEL", "SOURCE", "RESULT", "CONFIDENCE"):
            if field not in row:
                raise CheckError(f"{where}: missing required field {field}")
        source = _as_str(row["SOURCE"])
        if source not in ("BEFORE", "AFTER"):
            raise CheckError(f"{where}: SOURCE must be BEFORE or AFTER, got {source!r}")
        result = _as_str(row["RESULT"])
        if result not in ("PASS", "FAIL"):
            raise CheckError(f"{where}: RESULT must be PASS or FAIL, got {result!r}")
        confidence = _as_str(row["CONFIDENCE"])
        if confidence not in ("HIGH", "LOW"):
            raise CheckError(f"{where}: CONFIDENCE must be HIGH or LOW, got {confidence!r}")
        cnt = _count_field(row.get("CNT"), "CNT", where)
        total = _count_field(row.get("TOTAL"), "TOTAL", where)
        if cnt < 0 or total < 0:
            raise CheckError(f"{where}: CNT/TOTAL must not be negative")
        app_ids = row.get("APP_IDS") or []
        if not isinstance(app_ids, (list, tuple)):
            raise CheckError(f"{where}: APP_IDS must be a list")
        ids = [_as_str(a) for a in app_ids]
        if known_app_ids is not None:
            foreign = [a for a in ids if a not in known_app_ids]
            if foreign:
                raise CheckError(
                    f"{where}: APP_IDS contains applications outside this run: {foreign[:3]}"
                )
        metric = row.get("METRIC")
        if metric is not None and not isinstance(metric, (int, float)):
            raise CheckError(f"{where}: METRIC must be a number or null")
        out.append({
            "LABEL": _as_str(row["LABEL"]),
            "SOURCE": source,
            "ARGUMENT": _as_str(row.get("ARGUMENT")),
            "CNT": cnt,
            "TOTAL": total,
            "METRIC": None if metric is None else float(metric),
            "APP_IDS": ids,
            "RESULT": result,
            "CONFIDENCE": confidence,
        })
    return out


def execute(spec: CheckSpec, run_id: str, overrides: dict | None = None) -> dict[str, Any]:
    """Run one check in-process (tests, single checks).  The worker uses a
    subprocess with a read-only role instead."""
    from ..db.mart import Mart

    params = {**spec.params, **(overrides or {})}
    started = time.perf_counter()
    try:
        mart = Mart(run_id)
        rows = spec.entrypoint(mart, params)
        validated = validate_rows(rows or [], run_id)
        return {"state": "DONE", "rows": validated, "error": "",
                "duration_ms": int((time.perf_counter() - started) * 1000)}
    except Exception as exc:  # noqa: BLE001 - a technical failure is not a business FAIL
        return {"state": "ERROR", "rows": [], "error": f"{type(exc).__name__}: {exc}",
                "duration_ms": int((time.perf_counter() - started) * 1000)}
