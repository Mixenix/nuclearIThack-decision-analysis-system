"""User procedures and checks: the two code sources.

``workspace`` — text stored in the database and edited in the web editor.
``file``      — a ``.py`` file registered under ``SCRIPT_ROOT``.

Both go through the same loader, so neither can skip the contract check.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..checks.loader import (
    CheckError,
    _metadata_from_source,
    list_builtin_checks,
    list_builtin_checks_light,
    list_script_checks,
    load_from_file,
)
from ..config import load_settings
from ..db.connection import query, tx
from .errors import InvalidRequest, NotFound

PROCEDURE_STAGES = ("normalize", "compare", "aggregate")

#: built-in registered files shipped with the application
BUILTIN_FILES: dict[str, tuple[str, str]] = {
    "normalize": ("СПР: нормализация", "spr_normalize.py"),
    "compare": ("СПР: сравнение срезов", "spr_compare.py"),
    "aggregate": ("СПР: агрегация фактов", "spr_aggregate.py"),
}


def script_root() -> Path:
    return Path(load_settings().script_root).resolve()


def list_registered_files() -> list[dict[str, Any]]:
    root = script_root()
    out: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.py")):
        if path.name.startswith("_"):
            continue
        out.append({"path": path.name, "size": path.stat().st_size,
                    "modified": path.stat().st_mtime})
    return out


def read_registered_file(rel_path: str) -> str:
    root = script_root()
    candidate = (root / rel_path).resolve()
    if root not in candidate.parents and candidate != root:
        raise InvalidRequest(f"path escapes SCRIPT_ROOT: {rel_path}")
    if not candidate.exists():
        raise NotFound(f"file not found: {rel_path}")
    return candidate.read_text(encoding="utf-8")


def default_selection(stage: str) -> dict[str, Any]:
    label, filename = BUILTIN_FILES[stage]
    return {"source": "file", "ref": filename, "label": label, "params": {}}


def resolve_procedure(owner_id: str, stage: str, selection: dict[str, Any] | None) -> dict[str, Any]:
    """Return ``{code, source_kind, source_ref, params}`` for one stage.

    The code text is *materialised now*: a later edit must not silently change
    what an already started run executes (PLAN.md §11.4).
    """
    selection = selection or default_selection(stage)
    source = selection.get("source") or "file"
    params = selection.get("params") or {}
    if source == "file":
        ref = selection.get("ref") or BUILTIN_FILES[stage][1]
        code = read_registered_file(ref)
        return {"code": code, "source_kind": "file", "source_ref": ref, "params": params}
    if source == "workspace":
        procedure_id = selection.get("procedure_id")
        if not procedure_id:
            raise InvalidRequest(f"{stage}: workspace source needs procedure_id")
        rows = query(
            "SELECT id, code, params, name FROM ca.procedures"
            " WHERE id = %s AND owner_id = %s AND stage = %s",
            (procedure_id, owner_id, stage),
        )
        if not rows:
            raise NotFound(f"{stage}: procedure {procedure_id} not found")
        merged = {**(rows[0]["params"] or {}), **params}
        return {"code": rows[0]["code"] or "", "source_kind": "workspace",
                "source_ref": str(rows[0]["id"]), "params": merged}
    raise InvalidRequest(f"{stage}: unknown procedure source {source!r}")


def _registered_file(ref: str) -> Path:
    """Resolve a registered ``.py`` and refuse anything outside SCRIPT_ROOT."""
    settings = load_settings()
    root = Path(settings.script_root).resolve()
    candidate = Path(ref)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if not (resolved == root or resolved.is_relative_to(root)):
        raise InvalidRequest(f"check file {ref} is outside SCRIPT_ROOT")
    if not resolved.is_file():
        raise NotFound(f"check file not found: {ref}")
    return resolved



def resolve_checks(owner_id: str, selections: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Materialise the chosen checks, including their code."""
    if not selections:
        return []
    builtin = {spec.id: spec for spec in list_builtin_checks_light()}
    scripted = {spec.id: spec for spec in list_script_checks()}
    out: list[dict[str, Any]] = []
    for item in selections:
        check_id = str(item.get("id") or "")
        params = item.get("params") or {}
        source = item.get("source") or "builtin"
        if source == "workspace":
            procedure_id = item.get("procedure_id")
            if not procedure_id:
                raise InvalidRequest(f"check {check_id}: workspace source needs procedure_id")
            rows = query(
                "SELECT id, code, params, name FROM ca.procedures"
                " WHERE id = %s AND owner_id = %s AND stage = 'check'",
                (procedure_id, str(owner_id)),
            )
            if not rows:
                raise NotFound(f"check {check_id}: procedure {procedure_id} not found")
            out.append({"id": check_id, "label": rows[0]["name"],
                        "code": rows[0]["code"] or "",
                        "params": {**(rows[0]["params"] or {}), **params},
                        "source_kind": "workspace", "source_ref": str(rows[0]["id"])})
            continue
        if source == "file" and item.get("ref"):
            # Read the file, never import it: resolving a configuration happens in
            # the web process, and a module-level side effect there would freeze
            # every session (PLAN.md §23.1, "no user code in the UI process").
            # Metadata comes from the AST; the code is executed later, in the
            # worker's own interpreter.
            resolved = _registered_file(item["ref"])
            code = resolved.read_text(encoding="utf-8")
            meta = _metadata_from_source(code, str(resolved))
            if meta is None:
                raise InvalidRequest(
                    f"{resolved.name}: no CHECK declaration found; register a file that "
                    "exports CHECK = {'id','label','params','entrypoint'}")
            file_id, label, file_params = meta
            out.append({"id": file_id, "label": label, "code": code,
                        "params": {**file_params, **params},
                        "source_kind": "file", "source_ref": str(resolved)})
            continue
        spec = builtin.get(check_id) or scripted.get(check_id)
        if spec is None:
            raise NotFound(f"check {check_id} is not available")
        out.append({"id": spec.id, "label": spec.label, "code": spec.code,
                    "params": {**spec.params, **params},
                    "source_kind": spec.source_kind, "source_ref": spec.source_ref})
    return out


def available_checks(owner_id: str) -> list[dict[str, Any]]:
    """Everything the user may switch on: delivered checks, registered files, own code."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for spec in list_builtin_checks():
        out.append({"id": spec.id, "label": spec.label, "params": spec.params,
                    "source": "builtin", "source_ref": spec.source_ref})
        seen.add(spec.id)
    for spec in list_script_checks():
        if spec.id in seen:
            continue
        out.append({"id": spec.id, "label": spec.label, "params": spec.params,
                    "source": "file", "source_ref": spec.source_ref})
        seen.add(spec.id)
    for row in list_procedures(owner_id, "check"):
        out.append({"id": f"proc:{row['id']}", "label": row["name"],
                    "params": row["params"], "source": "workspace",
                    "procedure_id": row["id"]})
    return out


# ---------------------------------------------------------------------------
# CRUD for user procedures
# ---------------------------------------------------------------------------


def list_procedures(owner_id: str, stage: str | None = None) -> list[dict[str, Any]]:
    if stage:
        rows = query(
            "SELECT id, stage, name, source_kind, path_ref, params, updated_at"
            " FROM ca.procedures WHERE owner_id = %s AND stage = %s"
            " ORDER BY updated_at DESC", (owner_id, stage),
        )
    else:
        rows = query(
            "SELECT id, stage, name, source_kind, path_ref, params, updated_at"
            " FROM ca.procedures WHERE owner_id = %s ORDER BY stage, updated_at DESC",
            (owner_id,),
        )
    return [{**r, "id": str(r["id"])} for r in rows]


def get_procedure(owner_id: str, procedure_id: str) -> dict[str, Any]:
    rows = query(
        "SELECT id, stage, name, source_kind, code, path_ref, params, updated_at"
        " FROM ca.procedures WHERE id = %s AND owner_id = %s",
        (procedure_id, owner_id),
    )
    if not rows:
        raise NotFound("procedure not found")
    return {**rows[0], "id": str(rows[0]["id"])}


def save_procedure(owner_id: str, stage: str, name: str, source_kind: str,
                   code: str | None = None, path_ref: str | None = None,
                   params: dict | None = None,
                   procedure_id: str | None = None) -> str:
    if stage not in PROCEDURE_STAGES + ("check",):
        raise InvalidRequest(f"unknown stage {stage!r}")
    if code is not None:
        # Контракт проверяется и здесь, а не только кнопкой в интерфейсе: браузер можно
        # обойти, а сохранённый «почти правильный» код упадёт позже в воркере.
        from .workspace import validate_code
        verdict = validate_code(code, stage)
        if not verdict["ok"]:
            raise InvalidRequest(f"контракт нарушен: {verdict['message']}")
    payload = json.dumps(params or {})
    with tx() as conn, conn.cursor() as cur:
        if procedure_id:
            cur.execute(
                "UPDATE ca.procedures SET name = %s, source_kind = %s, code = %s,"
                " path_ref = %s, params = %s::jsonb, updated_at = now()"
                " WHERE id = %s AND owner_id = %s RETURNING id",
                (name, source_kind, code, path_ref, payload, procedure_id, owner_id),
            )
            row = cur.fetchone()
            if not row:
                raise NotFound("procedure not found")
            return str(row["id"])
        cur.execute(
            "INSERT INTO ca.procedures (owner_id, stage, name, source_kind, code,"
            " path_ref, params) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING id",
            (owner_id, stage, name, source_kind, code, path_ref, payload),
        )
        return str(cur.fetchone()["id"])


def delete_procedure(owner_id: str, procedure_id: str) -> None:
    with tx() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM ca.procedures WHERE id = %s AND owner_id = %s",
                    (procedure_id, owner_id))


def validate_check_source(code: str) -> dict[str, Any]:
    """Syntax + contract check without executing the check body."""
    from ..checks.loader import load_from_source

    try:
        spec = load_from_source(code, "workspace")
        return {"ok": True, "id": spec.id, "label": spec.label, "params": spec.params}
    except CheckError as exc:
        return {"ok": False, "error": str(exc)}
