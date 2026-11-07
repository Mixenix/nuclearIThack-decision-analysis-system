"""Примеры рабочей области должны **работать**, а не только проходить проверку контракта.

Здесь каждый пример исполняется так, как его исполнит воркер:

* проверки — на витрине синтетического прогона, с валидацией строки результата;
* ридер — на настоящем файле своего формата, с проверкой, что ядро получило тот же
  документ, что и из JSON;
* процедура нормализации — на паре файлов, с проверкой, что строки витрины совпадают
  с поставленной процедурой.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from change_analysis.checks.loader import load_from_source, validate_rows
from change_analysis.db.mart import Mart
from change_analysis.pipeline.core import normalize_document
from change_analysis.procedures.spr_normalize import DEFAULTS, _specs, read_document
from change_analysis.services.workspace import validate_code
from change_analysis.ui.workspace_examples import EXAMPLES, by_key
from qa_support import FakeContext, comparison_row as row, segment_row as segment

pytestmark = pytest.mark.integration

CHECK_EXAMPLES = [item for item in EXAMPLES if item["stage"] == "check"]


@pytest.fixture()
def mart(synthetic_run) -> str:
    """Маленькая витрина: правило сработало, решение перевернулось, порог сдвинулся."""
    rows = [
        row("", "A1", "decisionResult.product_trials.rules.hit", "15210|AGELOW@21",
            "false", "true", "VALUE_CHANGED"),
        row("", "A1", "decisionResult.product_trials.cutOff.value", "15210",
            "0.55", "0.62", "VALUE_CHANGED"),
        row("", "A1", "decisionResult.decision", "", "APPROVE", "REJECT", "VALUE_CHANGED"),
        row("", "A1", "decisionResult.available_offers.rank", "15210|standard",
            "1", "2", "VALUE_CHANGED"),
        row("", "A2", "characteristics.score_okb", "", "700", "650", "VALUE_CHANGED"),
        row("", "A2", "decisionResult.decision", "", "APPROVE", "REJECT", "VALUE_CHANGED"),
        row("", "A3", "decisionResult.product_trials.rules.name", "751|NOINCOME",
            "NOINCOME", None, "ATTRIBUTE_LOST"),
        row("", "A4", "decisionResult.decision", "", "APPROVE", "APPROVE", "SAME"),
    ]
    segments = [
        segment("", "A1", "APPROVE", "REJECT", score_okb_before="700", score_okb_after="700"),
        segment("", "A2", "APPROVE", "REJECT", score_okb_before="700", score_okb_after="650"),
        segment("", "A3", "APPROVE", "APPROVE"),
        segment("", "A4", "APPROVE", "APPROVE"),
    ]
    return synthetic_run("qa-workspace-examples", rows=rows, segments=segments)


# --- проверки: контракт + реальное исполнение ------------------------------

@pytest.mark.parametrize("example", CHECK_EXAMPLES, ids=[item["key"] for item in CHECK_EXAMPLES])
def test_check_example_executes_and_returns_a_valid_row(example: dict, mart: str) -> None:
    assert validate_code(example["code"], "check")["ok"]
    spec = load_from_source(example["code"], f"{example['key']}.py")
    rows = spec.entrypoint(Mart(mart), example["params"])
    assert isinstance(rows, list) and rows, "проверка должна вернуть хотя бы одну строку"
    validated = validate_rows(rows, mart, known_app_ids={"A1", "A2", "A3", "A4"})
    row_out = validated[0]
    assert row_out["LABEL"]
    assert row_out["RESULT"] in ("PASS", "FAIL")
    assert row_out["CONFIDENCE"] in ("HIGH", "LOW")
    assert 0 <= row_out["CNT"] <= row_out["TOTAL"]
    assert isinstance(row_out["APP_IDS"], list)


def test_stop_factor_example_finds_the_flipped_rule(mart: str) -> None:
    example = by_key("check_stop_factor_appeared")
    spec = load_from_source(example["code"], "stop.py")
    row_out = spec.entrypoint(Mart(mart), example["params"])[0]
    assert row_out["CNT"] == 1
    assert row_out["APP_IDS"] == ["A1"]
    assert row_out["RESULT"] == "FAIL"


def test_logic_flip_example_separates_logic_from_data(mart: str) -> None:
    example = by_key("check_logic_flip")
    spec = load_from_source(example["code"], "logic.py")
    row_out = spec.entrypoint(Mart(mart), example["params"])[0]
    # A1 — переворот без изменения скоров, A2 — вместе с изменением данных
    assert row_out["CNT"] == 1
    assert row_out["APP_IDS"] == ["A1"]


def test_lost_attributes_example_reports_the_missing_rule(mart: str) -> None:
    example = by_key("check_attribute_lost")
    spec = load_from_source(example["code"], "lost.py")
    row_out = spec.entrypoint(Mart(mart), example["params"])[0]
    assert row_out["CNT"] == 1
    assert "A3" in row_out["APP_IDS"]


# --- ридер: свой формат ----------------------------------------------------

READER_DOC = {
    "appData": {"application_id": "I1"},
    "decisionResult": {
        "decision": "APPROVE",
        "product_trials": [{"product_id": 15210, "cutOff": {"value": 0.62}}],
    },
}


def test_reader_example_parses_its_own_format(tmp_path: Path) -> None:
    example = by_key("reader_custom_format")
    assert validate_code(example["code"], "normalize")["kind"] == "reader"
    source = tmp_path / "app.txt"
    source.write_text(
        "\n".join([
            "# комментарий игнорируется",
            "appData.application_id = \"I1\"",
            "decisionResult.decision = \"APPROVE\"",
            "decisionResult.product_trials[0].product_id = 15210",
            "decisionResult.product_trials[0].cutOff.value = 0.62",
        ]) + "\n", encoding="utf-8")

    namespace = {"__name__": "reader_example"}
    exec(compile(example["code"], "reader_example.py", "exec"), namespace)
    assert namespace["read_document"](source) == READER_DOC


def test_reader_example_output_goes_through_the_kernel(tmp_path: Path) -> None:
    """Документ из своего формата должен дать те же строки витрины, что и JSON."""
    example = by_key("reader_custom_format")
    namespace: dict = {"__name__": "reader_example"}
    exec(compile(example["code"], "reader_example.py", "exec"), namespace)

    source = tmp_path / "app.txt"
    source.write_text(
        "appData.application_id = \"I1\"\n"
        "decisionResult.decision = \"APPROVE\"\n"
        "decisionResult.product_trials[0].product_id = 15210\n"
        "decisionResult.product_trials[0].cutOff.value = 0.62\n", encoding="utf-8")
    as_json = tmp_path / "app.json"
    as_json.write_text(json.dumps(READER_DOC), encoding="utf-8")

    specs = tuple(_specs(DEFAULTS))
    excluded = tuple(DEFAULTS["excluded_paths"])

    def rows_of(document):
        return [(r.path, r.array_key, r.value, r.value_type)
                for r in normalize_document(document, "BEFORE", "I1", array_specs=specs,
                                            excluded_paths=excluded)]

    assert rows_of(namespace["read_document"](source)) == rows_of(read_document(as_json))


# --- процедура нормализации: своя обёртка ---------------------------------

def test_normalize_procedure_example_produces_the_same_rows(tmp_path: Path) -> None:
    example = by_key("normalize_custom_rules")
    assert validate_code(example["code"], "normalize")["ok"]
    namespace: dict = {"__name__": "normalize_example"}
    exec(compile(example["code"], "normalize_example.py", "exec"), namespace)

    for side in ("before", "after"):
        (tmp_path / f"{side}.json").write_text(json.dumps(READER_DOC), encoding="utf-8")
    inputs = {"apps": [{"app_id": "I1",
                        "before": str(tmp_path / "before.json"),
                        "after": str(tmp_path / "after.json")}],
              "dataset_root": str(tmp_path)}

    context = FakeContext(artifact_dir=tmp_path)
    result = namespace["run"](context, inputs, example["params"])
    rows = list(result.rows)
    assert rows, "процедура должна вернуть строки витрины"
    assert {item["app_id"] for item in rows} == {"I1"}
    # технические поля исключены настройкой процедуры
    assert all("request_id" not in item["path"] for item in rows)


def test_normalize_procedure_example_keeps_the_delivered_arrays(tmp_path: Path) -> None:
    """Своя процедура не должна терять правила сопоставления массивов из поставки."""
    example = by_key("normalize_custom_rules")
    namespace: dict = {"__name__": "normalize_example"}
    exec(compile(example["code"], "normalize_example.py", "exec"), namespace)
    params = namespace["_params"]({})
    paths = {item["path"] for item in params["arrays"]}
    assert "decisionResult.product_trials.rules" in paths
    assert "decisionResult.available_offers" in paths
    assert params["excluded_paths"] == example["params"]["excluded_paths"]


# --- сохранение через сервис ------------------------------------------------

@pytest.mark.parametrize("example", EXAMPLES, ids=[item["key"] for item in EXAMPLES])
def test_every_example_saves_through_the_service(example: dict, owner: str) -> None:
    """Кнопка «Сохранить» идёт в сервис — значит сервис обязан принять каждый пример."""
    from change_analysis.services import procedures

    procedure_id = procedures.save_procedure(
        owner, example["stage"], example["title"], "workspace",
        code=example["code"], params=example["params"])
    try:
        listed = {item["id"] for item in procedures.list_procedures(owner)}
        assert procedure_id in listed
    finally:
        procedures.delete_procedure(owner, procedure_id)


def test_service_rejects_code_that_breaks_the_contract(owner: str) -> None:
    """Сервер не доверяет браузеру: битый контракт не сохраняется."""
    from change_analysis.services import procedures
    from change_analysis.services.errors import InvalidRequest

    with pytest.raises(InvalidRequest):
        procedures.save_procedure(
            owner, "check", "плохая проверка", "workspace",
            code="def run(m): return []\nCHECK = {'id': 'x'}", params={})
