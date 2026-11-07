"""Registered datasets (a run directory with two slices and a manifest)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import load_settings
from ..db.connection import query, tx
from .errors import InvalidRequest, NotFound


def _root() -> Path:
    return Path(load_settings().dataset_root).resolve()


def _root_for(abs_root: str | None) -> Path:
    """The root a dataset was registered under, not whatever is configured now."""
    return Path(abs_root).resolve() if abs_root else _root()


def dataset_dir(row: dict[str, Any]) -> Path:
    """Absolute directory of a registered dataset row (``abs_root`` + ``root_ref``)."""
    base = _root_for(row.get("abs_root"))
    candidate = (base / str(row["root_ref"])).resolve()
    if candidate != base and base not in candidate.parents:
        raise InvalidRequest(f"dataset path escapes its root: {row['root_ref']}")
    return candidate


def _safe_dir(root_ref: str, abs_root: str | None = None) -> Path:
    base = _root_for(abs_root)
    candidate = (base / root_ref).resolve()
    if candidate != base and base not in candidate.parents:
        raise InvalidRequest(f"dataset path escapes DATASET_ROOT: {root_ref}")
    return candidate


def precheck_dataset(root_ref: str, abs_root: str | None = None) -> dict[str, Any]:
    """Everything the config page needs before a run: pairs, manifest diff, issues."""
    try:
        run_dir = _safe_dir(root_ref, abs_root)
    except InvalidRequest as exc:
        return {"ok": False, "errors": [str(exc)], "warnings": [], "pairs": 0}
    manifest_path = run_dir / "manifest.json"
    errors: list[str] = []
    warnings: list[str] = []
    manifest: dict[str, Any] = {}
    if not manifest_path.exists():
        errors.append(f"manifest.json not found in {run_dir}")
        return {"ok": False, "errors": errors, "warnings": warnings, "pairs": 0,
                "run_dir": str(run_dir)}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.append(f"manifest.json is not valid JSON: {exc}")
        return {"ok": False, "errors": errors, "warnings": warnings, "pairs": 0,
                "run_dir": str(run_dir)}

    slices = manifest.get("srez") or {}
    before_rel = (slices.get("BEFORE") or {}).get("dir")
    after_rel = (slices.get("AFTER") or {}).get("dir")
    if not before_rel or not after_rel:
        errors.append("manifest has no srez.BEFORE.dir / srez.AFTER.dir")
        return {"ok": False, "errors": errors, "warnings": warnings, "pairs": 0,
                "run_dir": str(run_dir)}
    before_dir = run_dir / before_rel
    after_dir = run_dir / after_rel
    for path in (before_dir, after_dir):
        if not path.is_dir():
            errors.append(f"slice directory missing: {path}")
    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings, "pairs": 0,
                "run_dir": str(run_dir)}

    before_names = {p.stem for p in before_dir.glob("*.json")}
    after_names = {p.stem for p in after_dir.glob("*.json")}
    pairs = sorted(before_names & after_names)
    only_before = sorted(before_names - after_names)
    only_after = sorted(after_names - before_names)
    if only_before:
        errors.append(f"{len(only_before)} applications have no AFTER slice "
                      f"(e.g. {', '.join(only_before[:3])})")
    if only_after:
        errors.append(f"{len(only_after)} applications have no BEFORE slice "
                      f"(e.g. {', '.join(only_after[:3])})")
    declared = manifest.get("applications")
    if isinstance(declared, int) and declared != len(pairs):
        warnings.append(
            f"manifest declares {declared} applications but {len(pairs)} pairs were found"
        )
    technical = manifest.get("technical_fields_always_differ") or []
    return {
        "ok": not errors,
        "pairs": len(pairs),
        "before": len(before_names),
        "after": len(after_names),
        "declared": declared,
        "manifest": manifest,
        "technical_fields": technical,
        "run_dir": str(run_dir),
        "errors": errors,
        "warnings": warnings,
    }


def register_dataset(owner_id: str, name: str, root_ref: str) -> str:
    check = precheck_dataset(root_ref)
    if not check["ok"]:
        raise InvalidRequest("; ".join(check["errors"]))
    with tx() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ca.datasets (owner_id, name, root_ref, abs_root, manifest, app_pairs)"
            " VALUES (%s, %s, %s, %s, %s::jsonb, %s)"
            " ON CONFLICT (root_ref) DO UPDATE SET name = EXCLUDED.name,"
            " abs_root = EXCLUDED.abs_root, manifest = EXCLUDED.manifest,"
            " app_pairs = EXCLUDED.app_pairs"
            " RETURNING id",
            (owner_id, name, root_ref, str(_root()), json.dumps(check["manifest"]),
             check["pairs"]),
        )
        return str(cur.fetchone()["id"])


def list_datasets(owner_id: str) -> list[dict[str, Any]]:
    rows = query(
        "SELECT id, name, root_ref, abs_root, app_pairs, shared, manifest, created_at"
        " FROM ca.datasets WHERE owner_id = %s OR shared = true ORDER BY name",
        (owner_id,),
    )
    return [{**r, "id": str(r["id"])} for r in rows]


def get_dataset(owner_id: str, dataset_id: str) -> dict[str, Any]:
    rows = query(
        "SELECT * FROM ca.datasets WHERE id = %s AND (owner_id = %s OR shared = true)",
        (dataset_id, owner_id),
    )
    if not rows:
        raise NotFound("dataset not found")
    row = dict(rows[0])
    row["id"] = str(row["id"])
    return row


def discover_dataset_roots() -> list[dict[str, Any]]:
    """Every directory under DATASET_ROOT that looks like a run (has a manifest)."""
    base = _root()
    found: list[dict[str, Any]] = []
    if not base.is_dir():
        return found
    for manifest in sorted(base.glob("**/manifest.json")):
        if len(manifest.relative_to(base).parts) > 5:
            continue
        root_ref = str(manifest.parent.relative_to(base))
        check = precheck_dataset(root_ref)
        found.append({"root_ref": root_ref, "pairs": check["pairs"],
                      "ok": check["ok"], "errors": check["errors"]})
    return found


def ensure_delivered_dataset(owner_id: str) -> str | None:
    """Bootstrap: register the run shipped with the task package, if present."""
    for candidate in discover_dataset_roots():
        if candidate["ok"]:
            return register_dataset(owner_id, f"Демо-прогон {candidate['root_ref']}",
                                    candidate["root_ref"])
    return None
