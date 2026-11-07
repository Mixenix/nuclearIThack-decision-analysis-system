"""Saved configurations: everything needed to repeat a run."""

from __future__ import annotations

import json
from typing import Any

from ..db.connection import query, tx
from . import datasets, procedures
from .errors import AccessDenied, InvalidRequest, NotFound


def default_selections(owner_id: str) -> dict[str, Any]:
    """A working configuration: built-in procedures and the delivered checks on.

    The five delivered checks are the ones the task card asks for, so a fresh
    configuration starts with all of them enabled.  Otherwise "Сохранить и
    запустить" produces a run with an empty checks report, which looks like a
    broken report rather than a deliberate choice.
    """
    from ..checks.loader import list_builtin_checks

    try:
        checks = [{"id": spec.id, "params": dict(spec.params)}
                  for spec in list_builtin_checks()]
    except Exception:  # noqa: BLE001 - a missing task package must not break the page
        checks = []
    return {
        "procedures": {stage: procedures.default_selection(stage)
                       for stage in procedures.PROCEDURE_STAGES},
        "checks": checks,
        "params": {},
    }


def list_configurations(owner_id: str) -> list[dict[str, Any]]:
    rows = query(
        "SELECT c.id, c.name, c.dataset_id, c.selections, c.params, c.updated_at,"
        " d.name AS dataset_name FROM ca.configurations c"
        " LEFT JOIN ca.datasets d ON d.id = c.dataset_id"
        " WHERE c.owner_id = %s ORDER BY c.updated_at DESC",
        (owner_id,),
    )
    return [{**r, "id": str(r["id"]),
             "dataset_id": str(r["dataset_id"]) if r["dataset_id"] else None}
            for r in rows]


def save_configuration(
    owner_id: str,
    name: str,
    dataset_id: str,
    selections: dict[str, Any],
    params: dict[str, Any] | None = None,
    config_id: str | None = None,
) -> str:
    if not name.strip():
        raise InvalidRequest("configuration needs a name")
    if not dataset_id:
        raise InvalidRequest("configuration needs a dataset")
    datasets.get_dataset(owner_id, dataset_id)          # ownership / existence
    with tx() as conn, conn.cursor() as cur:
        if config_id:
            cur.execute(
                "UPDATE ca.configurations SET name = %s, dataset_id = %s, selections = %s::jsonb,"
                " params = %s::jsonb, updated_at = now()"
                " WHERE id = %s AND owner_id = %s RETURNING id",
                (name, dataset_id, json.dumps(selections), json.dumps(params or {}),
                 config_id, owner_id),
            )
            row = cur.fetchone()
            if not row:
                raise NotFound("configuration not found")
            return str(row["id"])
        cur.execute(
            "INSERT INTO ca.configurations (owner_id, dataset_id, name, selections, params)"
            " VALUES (%s,%s,%s,%s::jsonb,%s::jsonb) RETURNING id",
            (owner_id, dataset_id, name, json.dumps(selections), json.dumps(params or {})),
        )
        return str(cur.fetchone()["id"])


def load_configuration(owner_id: str, config_id: str) -> dict[str, Any]:
    rows = query(
        "SELECT * FROM ca.configurations WHERE id = %s AND owner_id = %s",
        (config_id, owner_id),
    )
    if not rows:
        raise NotFound("configuration not found")
    row = {**rows[0], "id": str(rows[0]["id"])}
    row["dataset_id"] = str(row["dataset_id"]) if row["dataset_id"] else None
    return row


def delete_configuration(owner_id: str, config_id: str) -> None:
    with tx() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM ca.configurations WHERE id = %s AND owner_id = %s",
                    (config_id, owner_id))


def configuration_form_options(owner_id: str) -> dict[str, Any]:
    """Everything the configuration page needs in one call."""
    return {
        "datasets": datasets.list_datasets(owner_id),
        "discovered": datasets.discover_dataset_roots(),
        "procedures": {stage: procedures.list_procedures(owner_id, stage)
                       for stage in procedures.PROCEDURE_STAGES},
        "registered_files": procedures.list_registered_files(),
        "checks": procedures.available_checks(owner_id),
        "builtin_files": procedures.BUILTIN_FILES,
    }
