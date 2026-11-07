"""Все поддерживаемые форматы читаются одной процедурой и дают тот же результат.

Ядро сравнения про формат не знает: процедура приводит файл к словарю. Тесты
фиксируют два обещания:

1. каждый заявленный формат действительно читается (включая составные расширения);
2. документ, записанный в любой «полный» формат, даёт **те же строки витрины**,
   что и исходный JSON — иначе поддержка формата была бы косметической.
"""

from __future__ import annotations

import gzip
import io
import json
import zipfile
from pathlib import Path

import pytest
import yaml

from change_analysis.pipeline.core import normalize_document
from change_analysis.procedures import spr_formats
from change_analysis.procedures.spr_normalize import DEFAULTS, _specs, read_document
from change_analysis.services.datasets import DEFAULT_FILE_GLOB, app_id_of, discover_files

pytestmark = pytest.mark.unit

DOC = {
    "appData": {"application_id": "I1", "createDateTime": "2026-08-27T00:00:00"},
    "decisionResult": {
        "decision": "APPROVE",
        "scoring": [{"modelName": "PD", "value": [0.397, 0.518]}],
        "product_trials": [{"product_id": 15210, "cutOff": 0.55,
                            "rules": [{"name": "AGELOW@21", "hit": False}]}],
    },
}

FLAT = {"appData": {"application_id": "I1"},
        "decisionResult": {"decision": "APPROVE", "cutOff": 0.55}}


def _rows(document: dict) -> list[tuple]:
    specs = tuple(_specs(DEFAULTS))
    return [(row.path, row.array_key, row.value, row.value_type)
            for row in normalize_document(document, "BEFORE", "X", array_specs=specs,
                                          excluded_paths=tuple(DEFAULTS["excluded_paths"]))]


def _write(tmp_path: Path, name: str, payload) -> Path:
    path = tmp_path / name
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(payload, encoding="utf-8")
    return path


@pytest.mark.parametrize("name,payload", [
    ("a.json", json.dumps(DOC)),
    ("a.jsonl", json.dumps(DOC) + "\n"),
    ("a.ndjson", json.dumps(DOC) + "\n\n"),
    ("a.yaml", yaml.safe_dump(DOC, allow_unicode=True, sort_keys=False)),
    ("a.yml", yaml.safe_dump(DOC, allow_unicode=True, sort_keys=False)),
    ("a.json.gz", gzip.compress(json.dumps(DOC).encode())),
    ("a.yaml.gz", gzip.compress(yaml.safe_dump(DOC).encode())),
])
def test_full_formats_give_the_same_document(tmp_path: Path, name: str, payload) -> None:
    path = _write(tmp_path, name, payload)
    assert spr_formats.read_any(path) == DOC


def test_zip_is_unwrapped(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.json", json.dumps(DOC))
    path = _write(tmp_path, "a.zip", buffer.getvalue())
    assert spr_formats.read_any(path) == DOC


@pytest.mark.parametrize("name,payload", [
    ("b.toml", 'appData.application_id = "I1"\n'
               '[decisionResult]\ndecision = "APPROVE"\ncutOff = 0.55\n'),
    ("b.ini", "[appData]\napplication_id = I1\n"
              "[decisionResult]\ndecision = APPROVE\ncutOff = 0.55\n"),
    ("b.properties", "appData.application_id=I1\n"
                     "decisionResult.decision=APPROVE\n"
                     "decisionResult.cutOff=0.55\n"),
    ("b.csv", "appData.application_id,decisionResult.decision,decisionResult.cutOff\n"
              "I1,APPROVE,0.55\n"),
    ("b.tsv", "appData.application_id\tdecisionResult.decision\tdecisionResult.cutOff\n"
              "I1\tAPPROVE\t0.55\n"),
])
def test_flat_formats_build_the_same_nesting(tmp_path: Path, name: str, payload) -> None:
    """TOML/INI/properties/CSV массивов не умеют, а вложенность через точку — умеют."""
    path = _write(tmp_path, name, payload)
    assert spr_formats.read_any(path) == FLAT


def test_unknown_extension_is_reported_with_the_supported_list(tmp_path: Path) -> None:
    path = _write(tmp_path, "a.parquet", b"\x00\x01")
    with pytest.raises(ValueError) as caught:
        spr_formats.read_any(path)
    message = str(caught.value)
    assert ".yaml" in message and ".zip" in message


def test_yaml_document_yields_identical_mart_rows(tmp_path: Path) -> None:
    reference = _rows(DOC)
    assert reference
    path = _write(tmp_path, "a.yaml", yaml.safe_dump(DOC, allow_unicode=True, sort_keys=False))
    assert _rows(read_document(path, {"input_format": "auto"})) == reference


def test_jsonl_document_yields_identical_mart_rows(tmp_path: Path) -> None:
    reference = _rows(DOC)
    path = _write(tmp_path, "a.jsonl", json.dumps(DOC) + "\n")
    assert _rows(read_document(path, {"input_format": "auto"})) == reference


def test_default_masks_cover_every_documented_extension() -> None:
    masks = DEFAULT_FILE_GLOB.split(",")
    assert len(masks) >= 15
    for expected in ("*.json", "*.xml", "*.yaml", "*.toml", "*.csv", "*.json.gz", "*.zip"):
        assert expected in masks


@pytest.mark.parametrize("name,app_id", [
    ("I1.json", "I1"), ("I1.jsonl", "I1"), ("I1.xml", "I1"), ("I1.yaml", "I1"),
    ("I1.json.gz", "I1"), ("I1.xml.gz", "I1"), ("I1.zip", "I1"), ("I1.csv", "I1"),
])
def test_app_id_ignores_compound_extensions(name: str, app_id: str) -> None:
    assert app_id_of(Path(name)) == app_id


def test_discovery_finds_mixed_extensions(tmp_path: Path) -> None:
    """В одном срезе могут лежать файлы разных форматов — это допустимо."""
    (tmp_path / "I1.json").write_text(json.dumps(DOC), encoding="utf-8")
    (tmp_path / "I2.yaml").write_text(yaml.safe_dump(DOC), encoding="utf-8")
    (tmp_path / "I3.json.gz").write_bytes(gzip.compress(json.dumps(DOC).encode()))
    found = discover_files(tmp_path, DEFAULT_FILE_GLOB)
    assert set(found) == {"I1", "I2", "I3"}


def test_duplicate_app_id_across_formats_is_refused(tmp_path: Path) -> None:
    (tmp_path / "I1.json").write_text(json.dumps(DOC), encoding="utf-8")
    (tmp_path / "I1.yaml").write_text(yaml.safe_dump(DOC), encoding="utf-8")
    with pytest.raises(Exception) as caught:
        discover_files(tmp_path, DEFAULT_FILE_GLOB)
    assert "duplicate APP_ID=I1" in str(caught.value)


def test_workspace_examples_compile_and_match_their_stage() -> None:
    from change_analysis.ui.workspace_examples import EXAMPLES

    assert len(EXAMPLES) >= 4
    for example in EXAMPLES:
        compile(example["code"], example["key"], "exec")
        assert example["stage"] in ("check", "normalize", "compare", "aggregate")
        assert example["title"] and example["about"]


def test_workspace_check_examples_declare_a_valid_contract() -> None:
    from change_analysis.checks.loader import load_from_source
    from change_analysis.ui.workspace_examples import EXAMPLES

    checks = [item for item in EXAMPLES if item["stage"] == "check"]
    assert len(checks) >= 3
    for example in checks:
        spec = load_from_source(example["code"], f"{example['key']}.py")
        assert spec.id and spec.label
        assert callable(spec.entrypoint)
