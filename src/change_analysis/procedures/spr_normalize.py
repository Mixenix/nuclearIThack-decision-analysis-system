"""СПР response normalization procedure.

This is *user code*: it is the only place that knows which arrays exist in a СПР
response, which fields are technical, and how numeric values are bucketed.  The
kernel (``change_analysis.pipeline.core``) knows only the generic table contract.

Two sources, one file: the same text can be edited in the web workspace and is
loaded by the same loader when selected as a registered ``.py`` file.
"""

from __future__ import annotations

import json
from pathlib import Path

from change_analysis.pipeline.core import (
    DEFAULT_ARRAY_SPECS,
    ArraySpec,
    BucketRule,
    NormalizationError,
    make_adjuster,
    normalize_document,
)
from change_analysis.pipeline.segments import build_product_segments, build_segment

DEFAULTS = {
    # technical fields: always differ, never substantive (task card trap #1)
    "excluded_paths": ["appData.request_id", "appData.createDateTime"],
    # array match modes (task card traps #2 and #3)
    "arrays": [
        {"path": "decisionResult.available_offers", "mode": "field",
         "key_fields": ["product_id", "type"]},
        {"path": "decisionResult.product_trials", "mode": "field",
         "key_fields": ["product_id"]},
        {"path": "decisionResult.product_trials.rules", "mode": "field",
         "key_fields": ["name"]},
        {"path": "decisionResult.product_trials.scoring.scoreModels", "mode": "field",
         "key_fields": ["modelName"]},
        {"path": "decisionResult.scoring", "mode": "field", "key_fields": ["modelName"]},
        {"path": "decisionResult.scoring.value", "mode": "index"},
        {"path": "ext_data", "mode": "field", "key_fields": ["body.action"]},
        {"path": "product_catalog", "mode": "field", "key_fields": ["mfo_id", "product_id"]},
        {"path": "tariff_matrix", "mode": "field", "key_fields": ["mfo_id", "product_id"]},
    ],
    # bucketing for the report grouping; identical boundaries for both slices,
    # DIFF_FLAG is never derived from the bucket
    "buckets": [
        {"path_prefix": "decisionResult.product_trials.scoring.scoreModels.value",
         "edges": [0.0, 0.3, 0.6, 0.9, 1.01]},
        {"path_prefix": "decisionResult.product_trials.scoring.pd.value",
         "edges": [0.0, 0.3, 0.6, 0.9, 1.01]},
        {"path_prefix": "decisionResult.product_trials.cutOff.value",
         "edges": [0.0, 0.3, 0.6, 0.9, 1.01]},
        {"path_prefix": "decisionResult.scoring.value",
         "edges": [0.0, 0.3, 0.6, 0.9, 1.01]},
        {"path_prefix": "characteristics.score_okb",
         "edges": [0, 500, 600, 700, 800, 900, 1001]},
        {"path_prefix": "characteristics.score_equ",
         "edges": [0, 500, 600, 700, 800, 900, 1001]},
    ],
    "max_errors_reported": 20,
}


def _specs(params: dict) -> list[ArraySpec]:
    raw = params.get("arrays")
    if not raw:
        return list(DEFAULT_ARRAY_SPECS)
    return [
        ArraySpec(item["path"], item["mode"], tuple(item.get("key_fields") or ()))
        for item in raw
    ]


def _adjuster(params: dict):
    raw = params.get("buckets") or []
    rules = [BucketRule(item["path_prefix"], tuple(float(e) for e in item["edges"]))
             for item in raw]
    return make_adjuster(rules) if rules else None


def read_document(path: Path, params: dict | None = None) -> dict:
    """Reader dispatch belongs to user code. Custom code is frozen by the service."""
    params = params or {}
    reader_code = params.get("reader_code")
    if reader_code:
        namespace = {"__name__": "input_reader"}
        exec(compile(reader_code, params.get("reader_ref", "reader.py"), "exec"), namespace)
        value = namespace["read_document"](path)
    else:
        kind = params.get("input_format", "auto")
        kind = path.suffix.lower().lstrip(".") if kind == "auto" else kind
        if kind == "json":
            value = json.loads(path.read_text(encoding="utf-8"))
        elif kind == "xml":
            from change_analysis.procedures.spr_normalize_xml import read_document as read_xml
            value = read_xml(path)
        else:
            raise ValueError(f"Unknown input format {kind!r} for {path.name}; "
                             "select a custom .py reader from SCRIPT_ROOT")
    if not isinstance(value, dict):
        raise ValueError(f"reader must return a dict: {path}")
    return value


def run(context, inputs, params):
    """inputs: {"apps": [{"app_id","before","after"}], "dataset_root": str}"""
    params = {**DEFAULTS, **(params or {})}
    specs = _specs(params)
    excluded = tuple(params.get("excluded_paths") or ())
    adjust = _adjuster(params)
    # Keep at least one ERROR so a reporting limit cannot hide a failed batch.
    max_errors = max(1, int(params.get("max_errors_reported", 20)))

    apps = inputs.get("apps") or []
    total = len(apps)
    diagnostics = []
    errors = 0
    processed = 0

    def normalized_stream():
        nonlocal errors, processed
        for index, item in enumerate(apps, start=1):
            app_id = item["app_id"]
            before_path = Path(item["before"])
            after_path = Path(item["after"])
            path = before_path
            try:
                before = read_document(before_path, params)
                path = after_path
                after = read_document(after_path, params)
            except (OSError, UnicodeError, ValueError) as exc:
                errors += 1
                if errors <= max_errors:
                    diagnostics.append(Diagnostic(**{
                        "level": "ERROR",
                        "message": f"APP_ID={app_id}, file={path}: unreadable response: {exc}",
                        "code": ("INCOMPLETE_PAIR" if isinstance(exc, FileNotFoundError)
                                 else "BAD_JSON" if isinstance(exc, json.JSONDecodeError)
                                 else "BAD_INPUT"),
                        "where": f"{app_id}",
                    }))
                context.log(f"cannot read pair {app_id}: {exc}")
                continue

            declared = (before.get("appData") or {}).get("application_id")
            if declared is not None and str(declared) != app_id:
                diagnostics.append(Diagnostic(**{
                    "level": "WARNING",
                    "message": f"appData.application_id={declared!r} does not match "
                               f"the file name {app_id!r}",
                    "code": "APP_ID_MISMATCH",
                    "where": app_id,
                }))

            for doc, source, path in ((before, "BEFORE", before_path),
                                      (after, "AFTER", after_path)):
                try:
                    rows = normalize_document(doc, source, app_id, specs, excluded, adjust)
                except NormalizationError as exc:
                    errors += 1
                    if errors <= max_errors:
                        diagnostics.append(Diagnostic(**{
                            "level": "ERROR", "message": f"file={path}: {exc}",
                            "code": "NORMALIZE_FAILED", "where": f"{app_id}:{source}",
                        }))
                    context.log(f"normalize failed {app_id}/{source}: {exc}")
                    continue
                for row in rows:
                    yield {
                        "app_id": row.app_id, "source": row.source,
                        "prefix": row.prefix, "cls": row.cls, "suffix": row.suffix,
                        "array_key": row.array_key, "key": row.key, "path": row.path,
                        "value": row.value, "value_type": row.value_type,
                        "adjusted_value": row.adjusted_value, "pointer": row.pointer,
                    }

            segments.append(build_segment(app_id, before, after))
            product_segments.extend(build_product_segments(app_id, before, after))
            processed += 1
            if processed % 25 == 0 or processed == total:
                context.progress(processed, total, f"paired {processed}/{total}")

    from change_analysis.contracts.types import Diagnostic, NormalizeResult

    segments: list[dict] = []
    product_segments: list[dict] = []
    # ``rows`` is a generator: the runner streams it straight into
    # normalized_rows.jsonl, so a 600k-row run never sits in memory as one list.
    # ``segments`` is filled while that generator is consumed, and the runner
    # writes rows first and segments afterwards — order matters here.
    # Share diagnostics too: they are populated only as rows are consumed.
    return NormalizeResult(
        rows=normalized_stream(),
        segments=segments,
        product_segments=product_segments,
        diagnostics=diagnostics,
    )


PROCEDURE = {
    "id": "spr_normalize_v1",
    "label": "Нормализация ответов СПР",
    "stage": "normalize",
    "contract_version": 1,
    "params": DEFAULTS,
    "entrypoint": run,
}
